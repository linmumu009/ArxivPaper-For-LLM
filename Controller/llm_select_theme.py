from __future__ import annotations

import argparse
import logging
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from openai import OpenAI

ROOT = Path(os.path.dirname(os.path.dirname(__file__)))
sys.path.insert(0, str(ROOT))

from config.config import (  # noqa: E402
    DATA_ROOT,
    qwen_api_key,
    theme_select_base_url,
    theme_select_model,
    theme_select_max_tokens,
    theme_select_temperature,
    theme_select_concurrency,
    theme_select_system_prompt,
)


def setup_logging() -> logging.Logger:
    logger = logging.getLogger("llm_select_theme")
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("[%(levelname)s] %(message)s")
    sh = logging.StreamHandler(stream=sys.stdout)
    sh.setFormatter(fmt)
    logger.addHandler(sh)

    log_root = ROOT / "logs" / datetime.now().strftime("%Y-%m-%d")
    log_root.mkdir(parents=True, exist_ok=True)
    log_file = log_root / (datetime.now().strftime("%H%M%S") + "_llm_select_theme.log")
    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s " + fmt._fmt))
    logger.addHandler(fh)
    return logger


def normalize_text(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip()


def find_latest_md(root: Path, explicit: Optional[str]) -> Path:
    if explicit:
        p = Path(explicit)
        if not p.exists():
            raise SystemExit(f"md not found: {p}")
        return p
    if not root.exists():
        raise SystemExit(f"md dir not found: {root}")
    files = sorted([p for p in root.glob("*.md") if p.is_file()])
    if not files:
        raise SystemExit(f"no markdown in {root}")
    return files[-1]


@dataclass
class PaperBlock:
    start: int
    end: int
    insert_after: int
    title: str
    abstract: str


def extract_title(line: str) -> str:
    start = line.find("**")
    end = line.rfind("**")
    if start != -1 and end != -1 and end > start + 2:
        return normalize_text(line[start + 2 : end])
    return normalize_text(line)


def extract_abstract(block_lines: List[str]) -> str:
    in_abs = False
    parts: List[str] = []
    for raw in block_lines:
        line = raw.strip()
        if line.startswith("- Abstract:"):
            in_abs = True
            continue
        if not in_abs:
            continue
        if "</details>" in line:
            break
        if "<details" in line or "<summary" in line:
            continue
        if not line:
            continue
        parts.append(line)
    return normalize_text(" ".join(parts))


def parse_blocks(lines: List[str]) -> List[PaperBlock]:
    blocks: List[PaperBlock] = []
    current_start: Optional[int] = None
    for idx, raw in enumerate(lines):
        line = raw.strip()
        if re.match(r"^\d+\.\s+\*\*", line):
            if current_start is not None:
                blocks.append(_build_block(lines, current_start, idx))
            current_start = idx
    if current_start is not None:
        blocks.append(_build_block(lines, current_start, len(lines)))
    return blocks


def _build_block(lines: List[str], start: int, end: int) -> PaperBlock:
    title_line = lines[start].strip()
    title = extract_title(title_line)
    insert_after = start
    for i in range(start, end):
        if lines[i].strip().startswith("- arXiv: ["):
            insert_after = i
            break
    abstract = extract_abstract(lines[start:end])
    return PaperBlock(
        start=start,
        end=end,
        insert_after=insert_after,
        title=title,
        abstract=abstract,
    )


def make_client() -> OpenAI:
    key = (qwen_api_key or "").strip()
    if not key:
        raise SystemExit("qwen_api_key missing in config.config")
    base = (theme_select_base_url or "").strip() or "https://dashscope.aliyuncs.com/compatible-mode/v1"
    return OpenAI(api_key=key, base_url=base)


def build_user_prompt(title: str, abstract: str) -> str:
    if abstract:
        return f"标题：{title}\n摘要：{abstract}"
    return f"标题：{title}\n摘要：无"


def parse_score(text: str) -> float:
    if not text:
        return 0.0
    m = re.search(r"([0-1](?:\.\d+)?)", text)
    if not m:
        return 0.0
    try:
        val = float(m.group(1))
    except ValueError:
        return 0.0
    if val < 0.0:
        return 0.0
    if val > 1.0:
        return 1.0
    return val


def score_one(client: OpenAI, block: PaperBlock) -> float:
    user_content = build_user_prompt(block.title, block.abstract)
    kwargs = {}
    if theme_select_temperature is not None:
        kwargs["temperature"] = float(theme_select_temperature)
    if theme_select_max_tokens is not None:
        kwargs["max_tokens"] = int(theme_select_max_tokens)
    resp = client.chat.completions.create(
        model=theme_select_model,
        messages=[
            {"role": "system", "content": theme_select_system_prompt},
            {"role": "user", "content": user_content},
        ],
        stream=False,
        **kwargs,
    )
    content = resp.choices[0].message.content if resp.choices else ""
    return parse_score(content)


def render_output(lines: List[str], scores: Dict[int, float]) -> List[str]:
    out_lines: List[str] = []
    for idx, line in enumerate(lines):
        out_lines.append(line)
        if idx in scores:
            score = scores[idx]
            out_lines.append(f"   - theme_relevant_score: {score:.3f}\n")
    return out_lines


def run() -> None:
    logger = setup_logging()
    print("============开始主题相关性评分==============", flush=True)
    ap = argparse.ArgumentParser("llm_select_theme")
    ap.add_argument("--md", default=None, help="input markdown from paperList_remove_duplications")
    ap.add_argument("--outdir", default=None, help="output dir (default data/llm_select_theme)")
    args = ap.parse_args()

    input_dir = ROOT / DATA_ROOT / "paperList_remove_duplications"
    md_path = find_latest_md(input_dir, args.md)
    out_dir = Path(args.outdir) if args.outdir else ROOT / DATA_ROOT / "llm_select_theme"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / md_path.name

    lines = md_path.read_text(encoding="utf-8", errors="ignore").splitlines(keepends=True)
    blocks = parse_blocks(lines)
    if not blocks:
        out_path.write_text("".join(lines), encoding="utf-8")
        logger.warning("No paper blocks found; wrote original content to %s", out_path)
        return

    client = make_client()
    scores: Dict[int, float] = {}
    workers = max(1, int(theme_select_concurrency or 1))
    logger.info("Scoring %d paper(s) with %d worker(s)", len(blocks), workers)

    total = len(blocks)
    done = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        future_map = {pool.submit(score_one, client, blk): blk for blk in blocks}
        for future in as_completed(future_map):
            blk = future_map[future]
            try:
                score = future.result()
            except Exception as exc:
                logger.warning("Score failed for %s: %r", blk.title, exc)
                score = 0.0
            scores[blk.insert_after] = score
            done += 1
            sys.stdout.write(f"\r[PROGRESS] scoring {done}/{total}")
            sys.stdout.flush()
            time.sleep(0.05)
    print()

    out_lines = render_output(lines, scores)
    out_path.write_text("".join(out_lines), encoding="utf-8")
    logger.info("Saved: %s", out_path)
    print("============结束主题相关性评分==============", flush=True)


if __name__ == "__main__":
    run()
