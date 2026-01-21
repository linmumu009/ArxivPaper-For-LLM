import argparse
import logging
import os
import re
import sys

from pypdf import PdfReader, PdfWriter

sys.path.append(os.path.dirname(os.path.dirname(__file__)))

from config.config import OUTPUT_DIR, FILENAME_FMT, PDF_OUTPUT_DIR, PDF_PREVIEW_DIR, USER_AGENT  # noqa: E402


def setup_logging():
    logger = logging.getLogger("pdf_split")
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("[%(levelname)s] %(message)s")
    sh = logging.StreamHandler(stream=sys.stdout)
    sh.setFormatter(fmt)
    logger.addHandler(sh)
    return logger


def detect_latest_md():
    if not os.path.isdir(OUTPUT_DIR):
        raise FileNotFoundError(f"OUTPUT_DIR not found: {OUTPUT_DIR}")
    files = [f for f in os.listdir(OUTPUT_DIR) if f.endswith(".md")]
    if not files:
        raise FileNotFoundError(f"No .md files found in {OUTPUT_DIR}")
    full = [os.path.join(OUTPUT_DIR, f) for f in files]
    full.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    return full[0]


def parse_arxiv_ids(md_path):
    ids = []
    pat = re.compile(r"\[(\d{4}\.\d{4,5})\]\(https://arxiv\.org/abs/\1")
    with open(md_path, "r", encoding="utf-8") as f:
        for line in f:
            m = pat.search(line)
            if m:
                ids.append(m.group(1))
    return ids


def split_pdf(in_path, out_path, pages, logger):
    if not os.path.exists(in_path):
        logger.warning("Source PDF not found, skip: %s", in_path)
        return False
    if os.path.exists(out_path):
        return False
    try:
        with open(in_path, "rb") as f:
            head = f.read(5)
        if not head.startswith(b"%PDF-"):
            logger.warning("Not a valid PDF header, skip: %s", in_path)
            return False
    except Exception as e:
        logger.error("Failed to inspect %s: %r", in_path, e)
        return False
    reader = PdfReader(in_path)
    writer = PdfWriter()
    count = min(pages, len(reader.pages))
    for i in range(count):
        writer.add_page(reader.pages[i])
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "wb") as f:
        writer.write(f)
    return True


def run():
    logger = setup_logging()
    ap = argparse.ArgumentParser()
    ap.add_argument("--md", default=None)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--pages", type=int, default=2)
    args = ap.parse_args()

    if args.md:
        md_path = args.md
    else:
        from datetime import datetime

        today = datetime.now().strftime(FILENAME_FMT)
        candidate = os.path.join(OUTPUT_DIR, today)
        if os.path.isfile(candidate):
            md_path = candidate
        else:
            md_path = detect_latest_md()

    logger.info("Use markdown list: %s", md_path)
    base = os.path.basename(md_path)
    date_str, _ = os.path.splitext(base)
    print("============开始切分预览 PDF==============", flush=True)

    arxiv_ids = parse_arxiv_ids(md_path)
    if args.limit is not None:
        arxiv_ids = arxiv_ids[: args.limit]

    total = len(arxiv_ids)
    logger.info("Total ids to split: %d", total)

    processed = 0
    skipped = 0

    for i, aid in enumerate(arxiv_ids, 1):
        src = os.path.join(PDF_OUTPUT_DIR, date_str, f"{aid}.pdf")
        dst = os.path.join(PDF_PREVIEW_DIR, date_str, f"{aid}.pdf")
        try:
            created = split_pdf(src, dst, args.pages, logger)
            if created:
                processed += 1
            else:
                skipped += 1
        except Exception as e:
            logger.error("Failed to split %s: %r", aid, e)
        msg = f"Splitting:【{i}/{total}】"
        if sys.stdout.isatty():
            sys.stdout.write(msg + "\r")
            sys.stdout.flush()
        else:
            print(msg, flush=True)

    if sys.stdout.isatty():
        sys.stdout.write("\n")
        sys.stdout.flush()
    logger.info("Done. created=%d, skipped=%d, total=%d", processed, skipped, total)
    print("============结束切分预览 PDF==============", flush=True)


if __name__ == "__main__":
    run()
