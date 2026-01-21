#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse
import logging
import re
import time
import traceback
from dataclasses import dataclass
from datetime import datetime, timedelta, time as dtime, timezone
from typing import Dict, List, Tuple, Optional

import requests
import feedparser
import os
import sys
sys.path.append(os.path.dirname(os.path.dirname(__file__)))
try:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(line_buffering=True)
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(line_buffering=True)
except Exception:
    pass
from config.config import (
    API_URL,
    SEARCH_CATEGORIES,
    SEARCH_TERMS,
    PATTERN_REGEX,
    GROUPS,
    ORDER_GROUPS,
    BUCKET_NAME_MAP,
    BUCKET_ORDER,
    USER_AGENT,
    DEFAULT_TZ,
    OUTPUT_DIR,
    FILENAME_FMT,
    PAGE_SIZE_DEFAULT,
    MAX_PAPERS_DEFAULT,
    SLEEP_DEFAULT,
    MIN_SCORE_DEFAULT,
    USE_PROXY_DEFAULT,
    RETRY_COUNT,
    PROGRESS_SINGLE_LINE,
)
from Controller.http_session import build_session

try:
    from zoneinfo import ZoneInfo  # py3.9+
except Exception:
    ZoneInfo = None

ARXIV_API = API_URL


def setup_logging():
    logger = logging.getLogger("arxiv")
    logger.setLevel(logging.INFO)

    fmt = logging.Formatter("[%(levelname)s] %(message)s")

    sh = logging.StreamHandler(stream=sys.stdout)
    sh.setFormatter(fmt)
    logger.addHandler(sh)

    project_root = os.path.dirname(os.path.dirname(__file__))
    log_root = os.path.join(project_root, "logs")
    date_dir = datetime.now().strftime("%Y-%m-%d")
    log_dir = os.path.join(log_root, date_dir)
    os.makedirs(log_dir, exist_ok=True)
    start_name = datetime.now().strftime("%H%M%S") + ".log"
    fh = logging.FileHandler(os.path.join(log_dir, start_name), encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s " + fmt._fmt))
    logger.addHandler(fh)

    return logger


def compile_patterns() -> Dict[str, re.Pattern]:
    compiled = {}
    for k, s in PATTERN_REGEX.items():
        compiled[k] = re.compile(s)
    return compiled


PATTERNS = compile_patterns()


def resolve_timezone(tz_name: Optional[str], logger):
    local_tz = datetime.now().astimezone().tzinfo
    if tz_name:
        if ZoneInfo is None:
            logger.warning("zoneinfo not available; fallback to system local timezone.")
            return local_tz
        try:
            return ZoneInfo(tz_name)
        except Exception:
            logger.warning("Failed to load timezone '%s', fallback to system local timezone.", tz_name)
            return local_tz
    return local_tz


def arxiv_id_from_entry_url(entry_id_url: str) -> str:
    m = re.search(r"/abs/([^v]+)(v\d+)?$", entry_id_url)
    return m.group(1) if m else entry_id_url


def entry_published_local_dt(entry, tzinfo) -> datetime:
    if hasattr(entry, "published_parsed") and entry.published_parsed:
        dt_utc = datetime(*entry.published_parsed[:6], tzinfo=timezone.utc)
    else:
        dt_utc = datetime.fromisoformat(entry.published.replace("Z", "+00:00"))
        if dt_utc.tzinfo is None:
            dt_utc = dt_utc.replace(tzinfo=timezone.utc)
    return dt_utc.astimezone(tzinfo)


def normalize_text(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip()


def build_arxiv_query() -> str:
    cats = "(" + " OR ".join([f"cat:{c}" for c in SEARCH_CATEGORIES]) + ")"
    q = "(" + " OR ".join(SEARCH_TERMS) + ")"
    return f"{cats} AND {q}"


def bucket_and_score(text: str) -> Tuple[str, int, Dict[str, int]]:
    hits = {}
    groups = GROUPS
    score = 0
    for k in groups:
        n = len(PATTERNS[k].findall(text))
        hits[k] = n
        if n > 0:
            score += 1
    order = ORDER_GROUPS
    names = BUCKET_NAME_MAP
    best = max(order, key=lambda k: hits[k])
    return names[best], score, hits


def fetch_page_with_retry(session: requests.Session, params: dict, logger, retries: int = 5):
    backoff = 1.0
    last_exc = None
    for attempt in range(1, retries + 1):
        try:
            r = session.get(ARXIV_API, params=params, timeout=60)
            r.raise_for_status()
            return feedparser.parse(r.text)
        except Exception as e:
            last_exc = e
            logger.warning("Request failed (attempt %d/%d): %s", attempt, retries, repr(e))
            if attempt < retries:
                time.sleep(backoff)
                backoff *= 2
    raise last_exc


@dataclass
class Paper:
    title: str
    published_local: datetime
    arxiv_id: str
    link: str
    bucket: str


def run():
    print("START arxiv_search.py", flush=True)
    logger = setup_logging()

    ap = argparse.ArgumentParser()
    ap.add_argument("--tz", default=None)
    ap.add_argument("--out", default=None)
    ap.add_argument("--page-size", type=int, default=PAGE_SIZE_DEFAULT)
    ap.add_argument("--max-papers", type=int, default=MAX_PAPERS_DEFAULT)
    ap.add_argument("--sleep", type=float, default=SLEEP_DEFAULT)
    ap.add_argument("--min-score", type=int, default=MIN_SCORE_DEFAULT)
    ap.add_argument("--use-proxy", action="store_true", default=USE_PROXY_DEFAULT, help="Allow using proxy from env (default: OFF)")
    ap.add_argument("--window-days", type=int, default=1)
    args = ap.parse_args()

    tz_name = args.tz if args.tz else DEFAULT_TZ
    tzinfo = resolve_timezone(tz_name, logger)

    now_local = datetime.now(tzinfo)
    days = max(1, int(getattr(args, "window_days", 1) or 1))
    yesterday = (now_local - timedelta(days=1)).date()
    start_date = yesterday - timedelta(days=days - 1)
    window_start = datetime.combine(start_date, dtime.min).replace(tzinfo=tzinfo)
    window_end = window_start + timedelta(days=days)

    logger.info("Timezone: %s", tzinfo)
    logger.info("Window  : %s -> %s", window_start.strftime("%Y-%m-%d %H:%M:%S %Z"), window_end.strftime("%Y-%m-%d %H:%M:%S %Z"))
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    out_filename = now_local.strftime(FILENAME_FMT)
    out_path = os.path.join(OUTPUT_DIR, out_filename)
    logger.info("Output  : %s", out_path)
    logger.info("min-score: %d", args.min_score)
    logger.info("Proxy from env enabled: %s", args.use_proxy)

    session = build_session(prefer_env_proxy=bool(args.use_proxy))

    query = build_arxiv_query()
    results: List[Paper] = []
    start_idx = 0
    page_size = max(1, min(args.page_size, 2000))
    candidates = 0
    pages = 0
    print("============开始获取初始可下载列表==============", flush=True)

    while len(results) < args.max_papers:
        pages += 1
        msg = f"[INFO] Fetch page 【{pages}】 (start={start_idx}, max_results={page_size}) ..."
        single_line = PROGRESS_SINGLE_LINE and sys.stdout.isatty()
        if single_line:
            sys.stdout.write(msg + "\r")
            sys.stdout.flush()
        else:
            print(msg, flush=True)

        params = {
            "search_query": query,
            "start": start_idx,
            "max_results": page_size,
            "sortBy": "submittedDate",
            "sortOrder": "descending",
        }
        feed = fetch_page_with_retry(session, params, logger, retries=RETRY_COUNT)

        if not feed.entries:
            logger.info("No entries returned; stopping.")
            break

        stop = False
        for entry in feed.entries:
            pub_local = entry_published_local_dt(entry, tzinfo)
            if pub_local < window_start:
                stop = True
                break
            if not (window_start <= pub_local < window_end):
                continue

            candidates += 1
            title = normalize_text(getattr(entry, "title", ""))
            abstract = normalize_text(getattr(entry, "summary", ""))
            full_text = f"{title}\n{abstract}"

            if not PATTERNS["CORE"].search(full_text):
                continue

            bucket, score, _hits = bucket_and_score(full_text)
            if score < args.min_score:
                continue

            arxiv_id = arxiv_id_from_entry_url(entry.id)
            link = f"https://arxiv.org/abs/{arxiv_id}"
            results.append(Paper(title=title, published_local=pub_local, arxiv_id=arxiv_id, link=link, bucket=bucket))

        if stop:
            logger.info("Reached entries older than window start; stopping.")
            break

        start_idx += page_size
        time.sleep(args.sleep)

    print()  # newline after progress
    print("============结束获取初始可下载列表==============", flush=True)
    results.sort(key=lambda p: p.published_local, reverse=True)

    buckets: Dict[str, List[Paper]] = {}
    for p in results:
        buckets.setdefault(p.bucket, []).append(p)

    with open(out_path, "w", encoding="utf-8") as f:
        f.write("# arXiv daily papers: LLM Training / RL / Agents / Text Algorithms\n\n")
        f.write(f"- Timezone: `{tzinfo}`\n")
        f.write(f"- Window: **{window_start.strftime('%Y-%m-%d %H:%M:%S %Z')}** to **{window_end.strftime('%Y-%m-%d %H:%M:%S %Z')}**\n")
        f.write(f"- Candidates in window: **{candidates}**\n")
        f.write(f"- Selected: **{len(results)}**\n")
        f.write(f"- min-score: `{args.min_score}`\n\n")

        if not results:
            f.write("_No matching papers found in this window._\n")
        else:
            order = BUCKET_ORDER
            for b in order:
                if b not in buckets:
                    continue
                f.write(f"## {b} (n={len(buckets[b])})\n\n")
                for i, p in enumerate(buckets[b], 1):
                    pub_str = p.published_local.strftime("%Y-%m-%d %H:%M:%S %Z")
                    f.write(f"{i}. **{p.title}**  \n")
                    f.write(f"   - Published: `{pub_str}`  \n")
                    f.write(f"   - arXiv: [{p.arxiv_id}]({p.link})\n\n")

    logger.info("Candidates in window: %d", candidates)
    logger.info("Selected papers     : %d", len(results))
    logger.info("Saved to            : %s", out_path)
    print("END arxiv_search.py", flush=True)


if __name__ == "__main__":
    try:
        run()
    except Exception:
        print("FATAL ERROR:\n" + traceback.format_exc(), flush=True)
        raise
