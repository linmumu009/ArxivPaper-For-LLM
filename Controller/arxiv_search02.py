#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
arXiv API 精确时间窗检索（UTC）
- 按 submittedDate date filter 在服务端过滤（再本地兜底校验）
- 语义查询：默认模拟 arxiv.org 搜索框（把自然语言拆词后用 all: 逐词 AND）
- 支持高级表达式：如果你输入里包含 ti:/abs:/AND/OR/括号/范围查询等，则原样拼接
- 支持分类：cat:xx 多类 OR
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import sys
import time
import traceback
from dataclasses import dataclass
from datetime import datetime, date, time as dtime, timedelta, timezone
from typing import Iterable, List, Optional, Tuple

import feedparser
import requests

try:
    from zoneinfo import ZoneInfo  # py3.9+
except Exception:
    ZoneInfo = None

try:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(line_buffering=True)
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(line_buffering=True)
except Exception:
    pass

sys.path.append(os.path.dirname(os.path.dirname(__file__)))
from config.config import USER_AGENT as CFG_UA  # noqa: E402
from Controller.http_session import build_session as shared_build_session  # noqa: E402

ARXIV_API_URL = "http://export.arxiv.org/api/query"

DATA_ROOT = "data"
OUTPUT_DIR = os.path.join(DATA_ROOT, "arxivList")
FILENAME_FMT = "%Y-%m-%d.md"
RETRY_COUNT = 7
PROGRESS_SINGLE_LINE = True


def setup_logging():
    logger = logging.getLogger("arxiv")
    logger.setLevel(logging.INFO)

    fmt = logging.Formatter("[%(levelname)s] %(message)s")

    sh = logging.StreamHandler(stream=sys.stdout)
    sh.setFormatter(fmt)
    logger.addHandler(sh)

    project_root = os.path.dirname(os.path.abspath(__file__))
    log_root = os.path.join(project_root, "logs")
    date_dir = datetime.now().strftime("%Y-%m-%d")
    log_dir = os.path.join(log_root, date_dir)
    os.makedirs(log_dir, exist_ok=True)
    start_name = datetime.now().strftime("%H%M%S") + ".log"
    fh = logging.FileHandler(os.path.join(log_dir, start_name), encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s " + fmt._fmt))
    logger.addHandler(fh)

    return logger


# -------------------------
# 1) 时间解析与 arXiv date filter
# -------------------------

def _parse_utc_datetime(s: str, *, is_end: bool) -> datetime:
    """
    解析用户输入的时间为 UTC datetime。

    支持：
    - YYYY-MM-DD            （start => 当天 00:00:00Z；end => 次日 00:00:00Z，便于“包含整天”）
    - YYYY-MM-DDTHH:MM:SSZ  / YYYY-MM-DDTHH:MM:SS+00:00 等 ISO8601
    """
    s = s.strip()

    # 仅日期：为“包含整天”做更符合直觉的处理
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        dt = datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        if is_end:
            dt = dt + timedelta(days=1)  # end 解释为“含当天”，所以取次日 00:00Z 作为右开边界
        return dt

    # ISO8601：把 Z 替换成 +00:00 方便 fromisoformat
    iso = s.replace("Z", "+00:00")
    dt = datetime.fromisoformat(iso)

    # 没带 tzinfo 就当 UTC（你明确要求全程 UTC）
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)

    return dt


def local_midnight_as_utc(anchor_tz: str, anchor_date: date) -> datetime:
    if ZoneInfo is None:
        raise RuntimeError("zoneinfo not available; cannot convert local midnight to UTC.")
    tz = ZoneInfo(anchor_tz)
    dt_local = datetime.combine(anchor_date, dtime(0, 0), tzinfo=tz)
    return dt_local.astimezone(timezone.utc)


def compute_window_by_midnight_anchor(
    *,
    anchor_tz: str,
    days: int,
    anchor_date_str: Optional[str],
) -> Tuple[datetime, datetime]:
    if days <= 0:
        raise ValueError("days 必须为正整数")

    if ZoneInfo is None:
        raise RuntimeError("zoneinfo not available; cannot compute anchor-based window.")

    tz = ZoneInfo(anchor_tz)
    if anchor_date_str:
        anchor_date = date.fromisoformat(anchor_date_str)
    else:
        anchor_date = datetime.now(tz).date()

    end_utc = local_midnight_as_utc(anchor_tz, anchor_date)
    start_utc = end_utc - timedelta(days=days)
    return start_utc, end_utc


def _floor_to_minute(dt: datetime) -> datetime:
    """向下取整到分钟（UTC）。"""
    dt = dt.astimezone(timezone.utc)
    return dt.replace(second=0, microsecond=0)


def _ceil_to_minute(dt: datetime) -> datetime:
    """向上取整到分钟（UTC）。"""
    dt = dt.astimezone(timezone.utc)
    if dt.second == 0 and dt.microsecond == 0:
        return dt
    dt = dt + timedelta(minutes=1)
    return dt.replace(second=0, microsecond=0)


def _to_arxiv_minute_str(dt: datetime) -> str:
    """
    arXiv submittedDate 过滤器：YYYYMMDDHHMM（手册说明精确到分钟，GMT/UTC）
    """
    dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y%m%d%H%M")


def build_submitted_date_clause(start_utc: datetime, end_utc: datetime) -> str:
    """
    构造 submittedDate:[START TO END] 子句。
    - start 用 floor(minute)
    - end 用 ceil(minute)（避免用户给了秒导致漏掉最后一分钟内的论文）
    """
    start_s = _to_arxiv_minute_str(_floor_to_minute(start_utc))
    end_s = _to_arxiv_minute_str(_ceil_to_minute(end_utc))
    return f"submittedDate:[{start_s} TO {end_s}]"


# -------------------------
# 2) “搜索框式语义查询” -> search_query 子句
# -------------------------

_ARXIV_FIELD_PREFIX_RE = re.compile(
    r"\b(?:ti|au|abs|co|jr|cat|rn|all|id|submittedDate|lastUpdatedDate)\s*:",
    re.IGNORECASE,
)
_ARXIV_BOOL_OR_GROUP_RE = re.compile(r"\b(?:AND|OR|ANDNOT)\b|[()\[\]]", re.IGNORECASE)


def is_advanced_arxiv_query(q: str) -> bool:
    """
    判断用户是否在写“高级表达式”：
    - 包含字段前缀 ti:/abs:/... 或 submittedDate:
    - 或包含 AND/OR/ANDNOT / 括号 / [] 范围符号
    """
    q = (q or "").strip()
    if not q:
        return False
    return bool(_ARXIV_FIELD_PREFIX_RE.search(q) or _ARXIV_BOOL_OR_GROUP_RE.search(q))


def semantic_query_to_all_clause(q: str) -> str:
    """
    把自然语言转换成更接近 arxiv.org 搜索框的行为：
    - 把输入拆成 token（支持 "..." 作为短语）
    - 每个 token 变成 all:token
    - token 之间用 AND 连接（更符合“搜索框输入多个词通常要求都出现”的直觉）

    注意：这不是“精确复刻 arxiv.org 前端全部逻辑”，但在 API 允许的范围内尽量贴近。
    """
    q = (q or "").strip()
    if not q:
        return ""

    # 解析：支持 "quoted phrase"
    tokens: List[Tuple[str, bool]] = []
    for m in re.finditer(r'"([^"]+)"|(\S+)', q):
        if m.group(1) is not None:
            tokens.append((m.group(1), True))   # 短语
        else:
            tokens.append((m.group(2), False))  # 单词

    # 清理空 token
    cleaned: List[Tuple[str, bool]] = []
    for t, is_phrase in tokens:
        t = t.strip()
        if t:
            cleaned.append((t, is_phrase))

    if not cleaned:
        return ""

    parts: List[str] = []
    for t, is_phrase in cleaned:
        if is_phrase:
            # 短语要加引号
            parts.append(f'all:"{t}"')
        else:
            parts.append(f"all:{t}")

    return " AND ".join(parts)


def build_text_clause(user_query: str) -> str:
    """
    构造文本查询子句：
    - 高级表达式：原样返回
    - 自然语言：转换成 all:... AND all:...
    """
    user_query = (user_query or "").strip()
    if not user_query:
        return ""
    if is_advanced_arxiv_query(user_query):
        return user_query
    return semantic_query_to_all_clause(user_query)


# -------------------------
# 3) 分类子句 + 总 search_query 拼装
# -------------------------

def build_category_clause(categories: List[str]) -> str:
    """
    构造分类过滤：
    (cat:cs.AI OR cat:cs.CL ...)
    """
    cats = [c.strip() for c in (categories or []) if c.strip()]
    if not cats:
        return ""
    inner = " OR ".join([f"cat:{c}" for c in cats])
    return f"({inner})"


def build_search_query(
    *,
    categories: List[str],
    user_query: str,
    start_utc: datetime,
    end_utc: datetime,
) -> str:
    """
    拼装最终 search_query：
    (cat:...) AND (文本...) AND submittedDate:[... TO ...]
    """
    clauses: List[str] = []

    cat_clause = build_category_clause(categories)
    if cat_clause:
        clauses.append(cat_clause)

    text_clause = build_text_clause(user_query)
    if text_clause:
        clauses.append(f"({text_clause})")

    date_clause = build_submitted_date_clause(start_utc, end_utc)
    clauses.append(date_clause)

    return " AND ".join(clauses)


# -------------------------
# 4) 拉取与解析（分页 + 重试）
# -------------------------

def build_session(*, user_agent: str, prefer_env_proxy: bool) -> requests.Session:
    """
    构造 requests Session
    - user_agent：建议显式设置
    - prefer_env_proxy：是否允许读取环境变量代理（如 HTTP_PROXY/HTTPS_PROXY）
    """
    s = requests.Session()
    s.headers.update({"User-Agent": user_agent})

    # 默认 requests 会读环境变量代理；如果你想强制禁用，可把 trust_env 设为 False
    s.trust_env = bool(prefer_env_proxy)
    return s


def request_feed(
    session: requests.Session,
    *,
    search_query: str,
    start: int,
    max_results: int,
    sort_by: str,
    sort_order: str,
    logger,
    timeout_sec: int = 60,
    retries: int = RETRY_COUNT,
) -> feedparser.FeedParserDict:
    """
    单页请求 + 指数退避重试
    """
    backoff = 3.0
    last_exc: Optional[Exception] = None

    params = {
        "search_query": search_query,
        "start": start,
        "max_results": max_results,
        "sortBy": sort_by,
        "sortOrder": sort_order,
    }

    for i in range(retries):
        try:
            r = session.get(ARXIV_API_URL, params=params, timeout=timeout_sec)
            r.raise_for_status()
            return feedparser.parse(r.text)
        except Exception as e:
            last_exc = e
            if logger:
                logger.warning("Request failed (attempt %d/%d): %s", i + 1, retries, repr(e))
            if i < retries - 1:
                time.sleep(backoff)
                backoff *= 2

    raise last_exc  # type: ignore[misc]


def parse_entry_published_utc(entry) -> datetime:
    """
    解析 entry 的 published 时间为 UTC datetime
    """
    if getattr(entry, "published_parsed", None):
        dt = datetime(*entry.published_parsed[:6], tzinfo=timezone.utc)
        return dt
    # 兜底：ISO 字符串
    dt = datetime.fromisoformat(entry.published.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def parse_arxiv_id(entry_id_url: str) -> Tuple[str, str]:
    """
    从 entry.id（形如 http://arxiv.org/abs/XXXX.XXXXvN 或旧式 math/xxxxxxxvN）
    拆出：
    - base_id：不带版本
    - version：vN（没有就空串）
    """
    # 取最后的 /abs/ 后内容
    m = re.search(r"/abs/(.+)$", entry_id_url)
    raw = m.group(1) if m else entry_id_url

    vm = re.search(r"(v\d+)$", raw)
    if vm:
        return raw[: -len(vm.group(1))], vm.group(1)
    return raw, ""


def extract_categories(entry) -> List[str]:
    """
    entry.tags: [{'term': 'cs.AI'}, ...]
    """
    tags = getattr(entry, "tags", None) or []
    out: List[str] = []
    for t in tags:
        term = t.get("term") if isinstance(t, dict) else None
        if term:
            out.append(term)
    return out


@dataclass
class Paper:
    title: str
    published_utc: datetime
    arxiv_id: str            # base id（不含 vN）
    version: str             # vN
    abs_url: str
    pdf_url: str
    categories: List[str]


def entry_to_paper(entry) -> Paper:
    """
    单条 entry -> Paper 结构（返回结构尽量贴近你 arxiv_search.py 的核心字段）
    """
    title = re.sub(r"\s+", " ", (getattr(entry, "title", "") or "")).strip()
    published_utc = parse_entry_published_utc(entry)

    base_id, version = parse_arxiv_id(entry.id)
    abs_url = f"https://arxiv.org/abs/{base_id}{version}"
    pdf_url = f"https://arxiv.org/pdf/{base_id}{version}.pdf"

    categories = extract_categories(entry)

    return Paper(
        title=title,
        published_utc=published_utc,
        arxiv_id=base_id,
        version=version,
        abs_url=abs_url,
        pdf_url=pdf_url,
        categories=categories,
    )


def fetch_papers_in_window(
    session: requests.Session,
    *,
    search_query: str,
    start_utc: datetime,
    end_utc: datetime,
    page_size: int,
    max_papers: int,
    sort_by: str = "submittedDate",
    sort_order: str = "descending",
    sleep_sec: float = 3.0,
    retries: int = RETRY_COUNT,
    logger=None,
    progress_single_line: bool = PROGRESS_SINGLE_LINE,
) -> Tuple[List[Paper], int]:
    """
    分页拉取并做 UTC 窗口过滤（start <= published < end）
    """
    page_size = max(1, min(int(page_size), 2000))
    max_papers = max(1, int(max_papers))

    results: List[Paper] = []
    candidates = 0
    start_idx = 0
    pages = 0

    while len(results) < max_papers:
        pages += 1
        msg = f"[INFO] Fetch page 【{pages}】 (start={start_idx}, max_results={page_size}) ..."
        single_line = progress_single_line and sys.stdout.isatty()
        if single_line:
            sys.stdout.write(msg + "\r")
            sys.stdout.flush()
        else:
            print(msg, flush=True)

        feed = request_feed(
            session,
            search_query=search_query,
            start=start_idx,
            max_results=page_size,
            sort_by=sort_by,
            sort_order=sort_order,
            logger=logger,
            retries=retries,
        )

        entries = feed.entries or []
        if not entries:
            if logger:
                logger.info("No entries returned; stopping.")
            break

        for entry in entries:
            p = entry_to_paper(entry)
            if start_utc <= p.published_utc < end_utc:
                candidates += 1
                results.append(p)
                if len(results) >= max_papers:
                    break

        start_idx += page_size
        time.sleep(max(0.0, float(sleep_sec)))

    if progress_single_line:
        print()

    # 统一按时间倒序
    results.sort(key=lambda x: x.published_utc, reverse=True)
    return results, candidates


# -------------------------
# 5) 输出（结构参考 arxiv_search.py：核心字段一致）
# -------------------------

def render_markdown(
    *,
    papers: List[Paper],
    start_utc: datetime,
    end_utc: datetime,
    search_query: str,
    candidates: int,
) -> str:
    """
    生成 Markdown 文本（你原来的脚本就是写 md 日报，这里保持风格相近）
    """
    lines: List[str] = []
    lines.append("# arXiv daily papers\n\n")
    lines.append(f"- Timezone: `UTC`")
    lines.append(
        f"- Window: **{start_utc.strftime('%Y-%m-%d %H:%M:%S UTC')}** to **{end_utc.strftime('%Y-%m-%d %H:%M:%S UTC')}**"
    )
    lines.append(f"- Candidates in window: **{candidates}**")
    lines.append(f"- Selected: **{len(papers)}**")
    lines.append(f"- search_query: `{search_query}`\n")

    if not papers:
        lines.append("_No matching papers found in this window._\n")
        return "\n".join(lines)

    for i, p in enumerate(papers, 1):
        pub = p.published_utc.strftime("%Y-%m-%d %H:%M:%S UTC")
        lines.append(f"{i}. **{p.title}**  ")
        lines.append(f"   - Published: `{pub}`  ")
        lines.append(f"   - arXiv: [{p.arxiv_id}](https://arxiv.org/abs/{p.arxiv_id})\n")

    return "\n".join(lines)


# -------------------------
# 6) CLI
# -------------------------

def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser()
    ap.add_argument("--query", "-q", default="", help="自然语言 or 高级表达式（ti:/abs:/AND/...）。自然语言将按 all: 逐词 AND 处理。")
    ap.add_argument("--categories", "-c", default="cs.CL,cs.LG,cs.AI,stat.ML", help="逗号分隔的分类，如 cs.AI,cs.LG")
    ap.add_argument("--start", required=False, default="", help="UTC 起始：YYYY-MM-DD 或 ISO8601（如 2026-01-19T00:00:00Z）")
    ap.add_argument("--end", required=False, default="", help="UTC 结束（右开）：YYYY-MM-DD 或 ISO8601。若是 YYYY-MM-DD，则解释为“包含该日”，自动+1天")
    ap.add_argument("--anchor-tz", default="Asia/Shanghai", help="锚定时区：以该时区的当天 00:00 换算为 UTC 作为 end")
    ap.add_argument("--days", type=int, default=1, help="当未提供 start/end 时，从锚定 00:00 往前推 days 天")
    ap.add_argument("--anchor-date", default="", help="锚定日期 YYYY-MM-DD（按 anchor-tz 的这天 00:00 作为 end）；为空则用 anchor-tz 的今天")
    ap.add_argument("--last-hours", type=float, default=None, help="可选：当未提供 start/end 时，用 now_utc - last_hours 到 now_utc（与锚定 00:00 互斥）")
    ap.add_argument("--page-size", type=int, default=200, help="每页数量（<=2000）")
    ap.add_argument("--max-papers", type=int, default=200, help="最多返回多少篇")
    ap.add_argument("--sleep", type=float, default=3.0, help="分页间隔秒数（建议 >=3，尊重 arXiv）")
    ap.add_argument("--retries", type=int, default=RETRY_COUNT, help="请求失败重试次数（指数退避）")
    ap.add_argument("--no-single-line-progress", action="store_true", help="禁用单行进度显示")
    ap.add_argument("--user-agent", default=CFG_UA, help="User-Agent（建议改成你自己的标识）")
    ap.add_argument("--use-proxy", action="store_true", help="是否允许读取环境变量代理")
    ap.add_argument("--out", default="", help="输出 markdown 文件路径；为空则写入 data/arxivList")
    return ap.parse_args()


def main() -> None:
    args = parse_args()
    print("START arxiv_search02.py", flush=True)
    logger = setup_logging()

    used_anchor_window = False
    anchor_date_for_name: Optional[date] = None

    if args.start.strip() and args.end.strip():
        start_utc = _parse_utc_datetime(args.start, is_end=False)
        end_utc = _parse_utc_datetime(args.end, is_end=True)
    else:
        if args.last_hours is not None:
            end_utc = datetime.now(timezone.utc)
            start_utc = end_utc - timedelta(hours=float(args.last_hours))
        else:
            used_anchor_window = True
            if ZoneInfo is not None:
                tz = ZoneInfo(str(args.anchor_tz))
                if args.anchor_date.strip():
                    anchor_date_for_name = date.fromisoformat(args.anchor_date.strip())
                else:
                    anchor_date_for_name = datetime.now(tz).date()
            start_utc, end_utc = compute_window_by_midnight_anchor(
                anchor_tz=str(args.anchor_tz),
                days=int(args.days),
                anchor_date_str=(args.anchor_date.strip() or None),
            )

    if end_utc <= start_utc:
        raise SystemExit(f"end 必须大于 start（当前 start={start_utc.isoformat()} end={end_utc.isoformat()}）")

    categories = [c.strip() for c in (args.categories or "").split(",") if c.strip()]
    search_query = build_search_query(
        categories=categories,
        user_query=args.query,
        start_utc=start_utc,
        end_utc=end_utc,
    )

    logger.info("Timezone: %s", "UTC")
    logger.info(
        "Window  : %s -> %s",
        start_utc.strftime("%Y-%m-%d %H:%M:%S UTC"),
        end_utc.strftime("%Y-%m-%d %H:%M:%S UTC"),
    )

    session = shared_build_session(prefer_env_proxy=bool(args.use_proxy))
    logger.info("Proxy from env enabled: %s", bool(args.use_proxy))

    papers, candidates = fetch_papers_in_window(
        session,
        search_query=search_query,
        start_utc=start_utc,
        end_utc=end_utc,
        page_size=args.page_size,
        max_papers=args.max_papers,
        sort_by="submittedDate",
        sort_order="descending",
        sleep_sec=args.sleep,
        retries=int(args.retries),
        logger=logger,
        progress_single_line=(PROGRESS_SINGLE_LINE and (not bool(args.no_single_line_progress))),
    )

    md = render_markdown(
        papers=papers,
        start_utc=start_utc,
        end_utc=end_utc,
        search_query=search_query,
        candidates=candidates,
    )

    out_path = args.out.strip()
    if not out_path:
        os.makedirs(OUTPUT_DIR, exist_ok=True)
        if used_anchor_window and anchor_date_for_name is not None:
            out_filename = anchor_date_for_name.strftime(FILENAME_FMT)
        else:
            out_filename = datetime.now(timezone.utc).strftime(FILENAME_FMT)
        out_path = os.path.join(OUTPUT_DIR, out_filename)

    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(md)
    logger.info("Candidates in window: %d", candidates)
    logger.info("Selected papers     : %d", len(papers))
    logger.info("Saved to            : %s", out_path)
    print("END arxiv_search02.py", flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        print("FATAL ERROR:\n" + traceback.format_exc(), flush=True)
        raise
