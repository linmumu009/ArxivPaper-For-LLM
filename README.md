<p align="right">
  <a href="./README.zh-CN.md">中文</a> | <strong>English</strong>
</p>



# ArxivPaper: A Reproducible Pipeline for Retrieval, Screening, and Summarization of arXiv Publications

ArxivPaper provides a practical, reproducible pipeline for daily retrieval of arXiv papers, automatic thematic screening using large language models (LLMs), PDF handling and parsing via MinerU, institution detection, and concise Chinese summary generation, with optional ingestion into Zotero. The design emphasizes transparent filtering criteria, deterministic windowing, and operational robustness (retry, idempotence, and concurrency controls).

---

## 1. Configuration

Only one file requires editing: `config/config.py`.

### 1.1 Required Credentials

- MinerU Token (`minerU_Token`): for PDF → Markdown parsing. Obtain from https://mineru.net/apiManage/token
- DashScope Key (`qwen_api_key`): for institution detection and summary generation. Obtain from https://bailian.console.aliyun.com/?spm=a2c4g.11186623.0.0.519511fceUTPZn&tab=model#/api-key

### 1.2 Models and Prompts (Key Parameters)

- Theme scoring (Controller/llm_select_theme.py)
  - `theme_select_base_url`: OpenAI-compatible base URL for the scoring model
  - `theme_select_model`: model name
  - `theme_select_max_tokens`: upper bound for output tokens
  - `theme_select_temperature`: sampling temperature
  - `theme_select_concurrency`: number of concurrent requests (threads)
  - `theme_select_system_prompt`: scoring instruction (expects a normalized 0–1 score)

- Institution detection (Controller/pdf_info.py)
  - `org_base_url`: OpenAI-compatible base URL
  - `org_model`: model name
  - `org_max_tokens`: upper bound for output tokens
  - `org_temperature`: sampling temperature
  - `pdf_info_system_prompt`: instruction producing structured JSON (institution, “is_large”, and short abstract points)

- Summarization (Controller/paper_summary.py)
  - `summary_base_url`: OpenAI-compatible base URL
  - `summary_model`: model name
  - `summary_max_tokens`: upper bound for output tokens
  - `summary_temperature`: sampling temperature
  - `summary_input_hard_limit`: hard input length cap (budget control)
  - `summary_input_safety_margin`: reserved margin for prompts/structure
  - `summary_concurrency`: number of concurrent requests (threads)
  - `summary_example`: example text (referenced by config.py)
  - `system_prompt`: system prompt defining final structure and style

---

## 2. Usage

### 2.1 Default run

```bash
python app.py
```

### 2.2 Run with parameters (examples)

```bash
# Example: natural-language query + specified timezone anchor
python app.py default --query "LLM alignment" --anchor-tz Asia/Shanghai
```

Note: any arguments following the pipeline name (e.g., `default` or `daily`) are forwarded only to Step 1 (`Controller/arxiv_search04.py`).

### 2.3 Command-line options

- `--query` (default `""`): natural language or advanced query (ti:/abs:/AND/…)
- `--categories` (default `SEARCH_CATEGORIES`): comma-separated categories
- `--start` (default `""`): UTC start (YYYY-MM-DD or ISO8601)
- `--end` (default `""`): UTC end (right-open; YYYY-MM-DD automatically +1 day)
- `--anchor-tz` (default `Asia/Shanghai`): define end by local midnight in this timezone
- `--days` (default `1`): when start/end unspecified, use [end − days, end) based on anchor midnight
- `--anchor-date` (default `""`): anchor date (YYYY-MM-DD)
- `--last-hours` (default `None`): alternative [now_utc − last_hours, now_utc) window (mutually exclusive with anchor settings)
- `--page-size` (default `PAGE_SIZE_DEFAULT`): page size (1–2000)
- `--max-papers` (default `MAX_PAPERS_DEFAULT`): maximum number of papers retained
- `--sleep` (default `SLEEP_DEFAULT`): pagination interval (seconds)
- `--use-proxy` (default `USE_PROXY_DEFAULT`): allow proxy from environment variables
- `--user-agent` (default `REQUESTS_UA`): User-Agent string
- `--out` (reserved): argument present but not used in the current version

---

## 3. Directory Layout

```text
ArxivPaper/                         # project root
├── README.en.md                    # this English README
├── README.zh-CN.md                 # Chinese README
├── app.py                          # orchestration: runs steps under Controller by pipeline
├── readmePrinceple.md              # notes and principles for authoring README
├── Controller/                     # core step scripts
│   ├── arxiv_search04.py           # Step 1: arXiv retrieval and time-window filtering
│   ├── paperList_remove_duplications.py  # Step 1.1: de-duplication and historical record
│   ├── llm_select_theme.py         # Step 2: LLM thematic relevance scoring
│   ├── paper_theme_filter.py       # Step 3: filter by relevance score
│   ├── pdf_download.py             # Step 4: download original PDFs
│   ├── pdf_split.py                # Step 5: slice preview pages
│   ├── pdfsplite_to_minerU.py      # Step 6: parse preview PDFs to Markdown via MinerU
│   ├── pdf_info.py                 # Step 7: institution detection and structured info
│   ├── instutions_filter.py        # Step 8: filter “large institution” papers
│   ├── selectpaper.py              # Step 9: move selected PDFs
│   ├── selectedpaper_to_mineru.py  # Step 10: parse full PDFs via MinerU
│   ├── paper_summary.py            # Step 11: generate Chinese summaries
│   ├── zotero_push.py              # Step 12: ingest selected papers into Zotero
│   └── http_session.py             # unified requests.Session with retry and headers
├── config/
│   ├── config.py                   # central configuration
│   └── paperList.json              # global “processed papers” list for de-duplication
├── data/
│   └── pdf_info/                   # structured institution JSON (by date)
└── logs/                           # run-time logs (by date)
```

---

## 4. Processing Pipeline (execution order)

### 0) Orchestration (`app.py`)

- Input: pipeline name and extra arguments
- Output: sequential execution of step scripts under `Controller/*.py` (Steps 1–12)
- Execution: read pipeline (default `default`), invoke each step via `subprocess.run()`, forward post-pipeline arguments only to Step 1

### 1) arXiv retrieval and window filtering (`Controller/arxiv_search04.py`)

- Inputs
  - Retrieval conditions: `SEARCH_CATEGORIES` + `--query`
  - Window and scale: `--start/--end/--anchor-*` + `PAGE_SIZE_DEFAULT/MAX_PAPERS_DEFAULT/SLEEP_DEFAULT`
- Outputs
  - Candidate list for the day: `data/arxivList/<date>.md`
- Logic
  - Compute the UTC window
  - Build query with `submittedDate:[START TO END]` for pagination ordering
  - Page with `sortBy=submittedDate` and `sortOrder=descending`
  - Filter by the Atom feed’s `published` timestamp only (converted to UTC); keep entries satisfying `window_start ≤ published_utc < window_end`
  - Write a Markdown report with window info, counts, and ordered entries

### 1.1) De-duplication and historical record (`Controller/paperList_remove_duplications.py`)

- Inputs
  - Daily candidate list (`data/arxivList/<date>.md`; default selects the latest)
  - Historical record (`config/paperList.json`; can be empty on first run)
- Outputs
  - Updated `config/paperList.json` containing `{title, source, writing_datetime}`
  - A de-duplicated Markdown from the daily list in `data/paperList_remove_duplications/<date>.md`
- Logic
  - Read existing records and construct a de-duplication key set `(title, source)`
  - Parse daily Markdown entries to extract titles and arXiv IDs
  - Append new records only when `(title, source)` not yet present
  - Rewrite a de-duplicated Markdown keeping section headers and metadata

### 2) LLM thematic relevance scoring (`Controller/llm_select_theme.py`)

- Input: de-duplicated list (`data/paperList_remove_duplications/<date>.md`) and scoring configuration (`theme_select_*`)
- Output: scored list (`data/llm_select_theme/<date>.md`) with `theme_relevant_score ∈ [0,1]`
- Logic: parse titles/abstracts and obtain scores via concurrent model calls, writing back into the original structure

### 3) Relevance-based filtering (`Controller/paper_theme_filter.py`)

- Input: scored list (`data/llm_select_theme/<date>.md`)
- Output: filtered list (`data/paper_theme_filter/<date>.md`)
- Logic: retain entries with `score ≥ threshold` while preserving header content

### 4) Original PDF download (`Controller/pdf_download.py`)

- Input: candidate list (`data/arxivList/<date>.md`)
- Output: original PDFs (`data/raw_pdf/<date>/<arxiv_id>.pdf`)
- Logic: extract arXiv IDs; skip if a valid PDF already exists (`%PDF-` header); otherwise download with retries, write to `.part`, validate, and atomically rename to `.pdf`

### 5) Preview slicing (`Controller/pdf_split.py`)

- Input: original PDFs
- Output: preview PDFs (first two pages) in `data/preview_pdf/<date>/<arxiv_id>.pdf`
- Logic: slice front pages; skip if already present

### 6) MinerU parsing for previews (`Controller/pdfsplite_to_minerU.py`)

- Inputs: preview PDFs and MinerU token
- Output: preview Markdown files in `data/preview_pdf_to_mineru/<date>/<arxiv_id>.md`
- Logic: perform batch processing (obtain upload URLs → PUT → poll → download zip → extract md); skip if output already exists

### 7) Institution detection and structured info (`Controller/pdf_info.py`)

- Inputs: preview Markdown (`data/preview_pdf_to_mineru/<date>/*.md`), metadata (title/published/arxiv_id) from the daily list, and model/prompt configuration (`org_*`, `pdf_info_system_prompt`)
- Output: structured JSON in `data/pdf_info/<date>.json` with fields `instution/is_large/abstract`
- Logic: concurrent model calls (default concurrency=8 configurable in `config/config.py`), merge metadata, deduplicate by `arxiv_id`

### 8) Large-institution filtering (`Controller/instutions_filter.py`)

- Input: `data/pdf_info/<date>.json`
- Output: PDF list containing only “large institution” papers at `data/instutions_filter/<date>/<date>.json`
- Logic: retain entries where `is_large == true`

### 9) Move selected PDFs (`Controller/selectpaper.py`)

- Inputs: large-institution list and original PDFs
- Output: selected PDFs in `data/selectedpaper/<date>/<arxiv_id>.pdf`
- Logic: parse IDs and move (`shutil.move`), removing sources from the original location

### 10) MinerU parsing for full PDFs (`Controller/selectedpaper_to_mineru.py`)

- Inputs: selected PDFs and MinerU token
- Output: Markdown in `data/selectedpaper_to_mineru/<date>/<arxiv_id>.md`
- Logic: batch parsing for full documents; skip if output exists

### 11) Chinese summary generation (`Controller/paper_summary.py`)

- Inputs: full-text Markdown and summarization configuration (`summary_*`, `system_prompt`)
- Outputs: single-article summaries (`data/paper_summary/single/<date>/<arxiv_id>.md`) and daily aggregates (`data/paper_summary/gather/<date>/<date>.txt`)
- Logic: budget-aware trimming, concurrent summarization, and aggregation

### 12) Zotero ingestion (`Controller/zotero_push.py`)

- Inputs: selected PDFs and per-article summaries
- Output: newly created Zotero items with attached files (no extra files locally)
- Logic: locate date-specific directories, construct items (title, abstract, arXiv link), call Zotero Connector endpoints for items and attachments, and provide terminal progress with final statistics

---

## 5. Notes on Time Windowing and Ordering

- All timestamps are treated in UTC unless otherwise stated.
- When using timezone anchoring (`--anchor-tz`), the end of the window is the midnight (00:00) of the specified timezone, converted to UTC.
- Entries are paginated by arXiv `submittedDate` in descending order for efficient retrieval.
- Filtering is applied to the Atom feed’s `published` timestamp (first-version submission), not the submittedDate field; this ensures correct window semantics while the sort order aids efficient pagination.

---

## 6. Ethical Use and Rate Limits

- Respect arXiv’s usage policies and avoid aggressive scraping; retain sensible pagination intervals (`--sleep`) and reasonable `--page-size`.
- Always provide a clear, identifiable `User-Agent` string (`--user-agent`) and enable proxy only when necessary.

