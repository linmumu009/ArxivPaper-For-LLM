from __future__ import annotations

import argparse
import logging
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import List, Optional

ROOT = Path(os.path.dirname(os.path.dirname(__file__)))
sys.path.insert(0, str(ROOT))

from config.config import DATA_ROOT  # noqa: E402


def setup_logging() -> logging.Logger:
    logger = logging.getLogger("paper_theme_filter")
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("[%(levelname)s] %(message)s")
    sh = logging.StreamHandler(stream=sys.stdout)
    sh.setFormatter(fmt)
    logger.addHandler(sh)

    log_root = ROOT / "logs" / datetime.now().strftime("%Y-%m-%d")
    log_root.mkdir(parents=True, exist_ok=True)
    log_file = log_root / (datetime.now().strftime("%H%M%S") + "_paper_theme_filter.log")
    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s " + fmt._fmt))
    logger.addHandler(fh)
    return logger


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
class Block:
    start: int
    end: int
    score: float


def parse_score(lines: List[str]) -> float:
    for raw in lines:
        line = raw.strip()
        if "theme_relevant_score" not in line:
            continue
        m = re.search(r"theme_relevant_score:\s*([0-1](?:\.\d+)?)", line)
        if not m:
            continue
        try:
            return float(m.group(1))
        except ValueError:
            return 0.0
    return 0.0


def collect_blocks(lines: List[str]) -> List[Block]:
    blocks: List[Block] = []
    current_start: Optional[int] = None
    for idx, raw in enumerate(lines):
        if re.match(r"^\d+\.\s+\*\*", raw.strip()):
            if current_start is not None:
                score = parse_score(lines[current_start:idx])
                blocks.append(Block(start=current_start, end=idx, score=score))
            current_start = idx
    if current_start is not None:
        score = parse_score(lines[current_start:])
        blocks.append(Block(start=current_start, end=len(lines), score=score))
    return blocks


def render_filtered(lines: List[str], blocks: List[Block], threshold: float) -> List[str]:
    keep_ranges: List[tuple[int, int]] = []
    total = len(blocks)
    for idx, b in enumerate(blocks, 1):
        if b.score >= threshold:
            keep_ranges.append((b.start, b.end))
        sys.stdout.write(f"\r[PROGRESS] filtering {idx}/{total}")
        sys.stdout.flush()
    print()

    header_end = blocks[0].start if blocks else len(lines)
    out_lines: List[str] = lines[:header_end]
    if not keep_ranges:
        return out_lines

    for start, end in keep_ranges:
        out_lines.extend(lines[start:end])
    return out_lines


def run() -> None:
    logger = setup_logging()
    print("============开始主题相关性过滤==============", flush=True)
    ap = argparse.ArgumentParser("paper_theme_filter")
    ap.add_argument("--md", default=None, help="input markdown from llm_select_theme")
    ap.add_argument("--outdir", default=None, help="output dir (default data/paper_theme_filter)")
    ap.add_argument("--threshold", type=float, default=0.85, help="score threshold to keep (default 0.85)")
    args = ap.parse_args()

    input_dir = ROOT / DATA_ROOT / "llm_select_theme"
    md_path = find_latest_md(input_dir, args.md)
    out_dir = Path(args.outdir) if args.outdir else ROOT / DATA_ROOT / "paper_theme_filter"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / md_path.name

    lines = md_path.read_text(encoding="utf-8", errors="ignore").splitlines(keepends=True)
    blocks = collect_blocks(lines)
    if not blocks:
        out_path.write_text("".join(lines), encoding="utf-8")
        logger.warning("No paper blocks found; wrote original content to %s", out_path)
        return

    kept_count = sum(1 for b in blocks if b.score >= float(args.threshold))
    filtered_count = len(blocks) - kept_count
    filtered = render_filtered(lines, blocks, float(args.threshold))
    out_path.write_text("".join(filtered), encoding="utf-8")
    logger.info("Saved: %s", out_path)
    print(f"[INFO] Kept: {kept_count}, Filtered: {filtered_count}", flush=True)
    print("============结束主题相关性过滤==============", flush=True)


if __name__ == "__main__":
    run()
