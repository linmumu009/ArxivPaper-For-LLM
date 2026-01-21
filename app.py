import os
import sys
import subprocess

ROOT = os.path.dirname(__file__)

STEPS = {
    "arxiv_search": [sys.executable, "-u", os.path.join(ROOT, "Controller", "arxiv_search04.py")],
    "paperList_remove_duplications": [sys.executable, "-u", os.path.join(ROOT, "Controller", "paperList_remove_duplications.py")],
    "llm_select_theme": [sys.executable, "-u", os.path.join(ROOT, "Controller", "llm_select_theme.py")],
    "paper_theme_filter": [sys.executable, "-u", os.path.join(ROOT, "Controller", "paper_theme_filter.py")],
    "pdf_download": [sys.executable, "-u", os.path.join(ROOT, "Controller", "pdf_download.py")],
    "pdf_split": [sys.executable, "-u", os.path.join(ROOT, "Controller", "pdf_split.py")],
    "pdfsplite_to_minerU": [sys.executable, "-u", os.path.join(ROOT, "Controller", "pdfsplite_to_minerU.py")],
    "pdf_info": [sys.executable, "-u", os.path.join(ROOT, "Controller", "pdf_info.py")],
    "instutions_filter": [sys.executable, "-u", os.path.join(ROOT, "Controller", "instutions_filter.py")],
    "selectpaper": [sys.executable, "-u", os.path.join(ROOT, "Controller", "selectpaper.py")],
    "selectedpaper_to_mineru": [sys.executable, "-u", os.path.join(ROOT, "Controller", "selectedpaper_to_mineru.py")],
    "paper_summary": [sys.executable, "-u", os.path.join(ROOT, "Controller", "paper_summary.py")],
    "zotero_push": [sys.executable, "-u", os.path.join(ROOT, "Controller", "zotero_push.py")],
}


PIPELINES = {
    "default": [
        "arxiv_search",
        "paperList_remove_duplications",
        "llm_select_theme",
        "paper_theme_filter",
        "pdf_download",
        "pdf_split",
        "pdfsplite_to_minerU",
        "pdf_info",
        "instutions_filter",
        "selectpaper",
        "selectedpaper_to_mineru",
        "paper_summary",
        "zotero_push",
    ],
    "daily": [
        "arxiv_search",
        "paperList_remove_duplications",
        "llm_select_theme",
        "paper_theme_filter",
        "pdf_download",
        "pdf_split",
        "pdfsplite_to_minerU",
        "pdf_info",
        "instutions_filter",
        "selectpaper",
        "selectedpaper_to_mineru",
        "paper_summary",
        "zotero_push",
    ],
}


def run_step(name, extra_args=None):
    if name not in STEPS:
        raise SystemExit(f"Unknown step: {name}")
    cmd = STEPS[name] + (extra_args or [])
    r = subprocess.run(cmd, check=True)
    return r.returncode


def detect_selected_count():
    data_root = os.path.join(ROOT, "data", "arxivList")
    if not os.path.isdir(data_root):
        return None
    files = [os.path.join(data_root, f) for f in os.listdir(data_root) if f.endswith(".md")]
    if not files:
        return None
    files.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    latest = files[0]
    try:
        with open(latest, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith("- Selected"):
                    # line format: - Selected: **N**
                    parts = line.split("**")
                    if len(parts) >= 2:
                        try:
                            return int(parts[1])
                        except ValueError:
                            return None
    except OSError:
        return None
    return None


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    pipeline = "default"
    extra = []
    if argv:
        pipeline = argv[0]
        extra = argv[1:]
    steps = PIPELINES.get(pipeline)
    if not steps:
        raise SystemExit(f"Unknown pipeline: {pipeline}")
    print(f"START pipeline '{pipeline}' with {len(steps)} step(s)", flush=True)
    for i, step in enumerate(steps):
        if i == 0:
            step_args = extra
        else:
            step_args = []
        print(f"RUN step: {step}", flush=True)
        run_step(step, step_args)
        if step == "arxiv_search":
            selected = detect_selected_count()
            if selected == 0:
                print("[PIPELINE] No papers selected in current window; stop after arxiv_search.", flush=True)
                return


if __name__ == "__main__":
    main()
