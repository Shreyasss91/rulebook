# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

This is a **local-first RAG (Retrieval-Augmented Generation) system** for querying KERC (Karnataka Electricity Regulatory Commission) regulatory documents. The corpus consists of 994 PDFs (25,453 pages) across multiple categories.

**Goal**: Natural language queries → cited answers with source file + page numbers.

## Architecture

```
PDFs → Text Extraction → OCR Fallback → Chunking → Embeddings → Vector DB → LLM Query → Cited Answers
```

**Stack**:
- **Vector DB**: ChromaDB (local, `kerch_db/`)
- **Embeddings**: sentence-transformers (all-MiniLM-L6-v2)
- **LLM**: Ollama (local) or Claude API
- **OCR**: Tesseract via `pytesseract`/`ocrmypdf` + Novita.ai DeepSeek OCR 2 API ($0.03/M tokens)
- **Text Extraction**: pdfplumber, python-docx
- **Chunking**: Legal-aware (preserves rule/section numbering)
- **Config**: YAML-driven (`config.yaml`)

Stack options (Vector DB, Embeddings) with pros/cons, the bilingual-retrieval gap and the re-index
migration plan live in `docs/stack_choices.md` — read it before changing `embedding_model` or
`collection_name`.

## Repository Layout

```
rule_books/
├── config.yaml                              # Central configuration
├── requirements.txt                         # Python dependencies (planned ones commented)
├── CHANGELOG.md                             # Keep a Changelog format, latest-first
├── CLAUDE.md                                # This file
├── docs/
│   ├── origin_doc.md                        # Architecture, OCR comparison, Option A vs B
│   ├── kerc_folder_inventory.md             # Corpus scan and cost projections
│   ├── deduplication_report.md              # Duplicate group details
│   ├── deduplication_ignore_list.json       # Committed: files to skip during ingestion
│   ├── deduplication_progress.json          # Gitignored: dedup resume state
│   ├── incremental_update_strategy.md       # Manifest-based incremental pipeline design
│   ├── ocr_pass_design.md                    # OCR pass design, decision log + implementation notes
│   ├── query_cli_design.md                   # Query CLI design (retrieval, citations, LLMs)
│   └── stack_choices.md                      # VectorDB / Embeddings options + recommendation
└── scripts/
    ├── scan_extensions.py
    ├── create_deduplication_ignore_list_v2.py
    ├── incremental_ingest.py                 # manifest-based change detection + indexing
    └── query_cli.py                          # cited Q&A over the indexed collection
```

Gitignored and generated at runtime: PDFs, `kerch_db/` (ChromaDB), `data/`, `docs/file_manifest.json`.

## Key Files

| File | Purpose |
|------|---------|
| `config.yaml` | Central config: `docs_source` (list), `file_types` (priority tiers), ignore list, progress, batch size |
| `scripts/create_deduplication_ignore_list_v2.py` | Content-based deduplication (size + first/last page hash) → `docs/deduplication_ignore_list.json` |
| `scripts/scan_extensions.py` | Recursive scan of `docs_source` for extension counts |
| `scripts/incremental_ingest.py` | Incremental pipeline: manifest diff → extract → OCR → chunk → embed → ChromaDB upsert |
| `scripts/query_cli.py` | Query CLI: embed question → vector search → cited answer (Ollama/Claude/extractive) |
| `docs/query_cli_design.md` | Query CLI design: retrieval, page-exact citations, LLM backends, failure modes |
| `docs/stack_choices.md` | Vector DB + embedding options with pros/cons, the Kannada gap, and the re-index migration plan |
| `docs/origin_doc.md` | Complete architecture, OCR comparison, hardware assessment, Option A vs B |
| `docs/incremental_update_strategy.md` | Manifest-based incremental pipeline design |
| `docs/ocr_pass_design.md` | OCR pass design, decision log and implementation notes; runs inside `scripts/incremental_ingest.py` |
| `docs/kerc_folder_inventory.md` | Corpus scan: 994 PDFs, 25K pages, category breakdown, cost projections |
| `docs/deduplication_report.md` | 55 duplicate groups detailed |
| `docs/deduplication_ignore_list.json` | 55 file paths to skip during ingestion |
| `CHANGELOG.md` | Version history (Keep a Changelog, latest-first). **Every change must be logged here** — see Conventions |
| `requirements.txt` | Dependencies. Active block is installed by default; planned deps stay commented so a fresh install works today |

## Commands

Run scripts from the project root — they resolve `config.yaml` as a relative path.

```bash
# Install dependencies (see requirements.txt; pdfplumber/python-docx enable
# per-type signatures, everything else is commented until its milestone)
pip install -r requirements.txt

# Scan extensions in configured sources
python scripts/scan_extensions.py

# Run deduplication (creates/updates ignore list)
python scripts/create_deduplication_ignore_list_v2.py

# Incremental ingest: report changes without writing anything
python scripts/incremental_ingest.py --dry-run -v

# Incremental ingest: measure what extraction would produce (needs_ocr, empty,
# unsupported counts) without embedding or writing anything
python scripts/incremental_ingest.py --audit

# Incremental ingest: apply them (runs the OCR pass when an engine is available)
python scripts/incremental_ingest.py

# OCR pass controls: --no-ocr skips it, --ocr-limit N caps pages this run
# (--dry-run/--audit never OCR, so they cannot spend anything)
python scripts/incremental_ingest.py --no-ocr
python scripts/incremental_ingest.py --ocr-limit 200

# Query the indexed corpus with page-exact citations (see docs/query_cli_design.md)
python scripts/query_cli.py "what is the late payment surcharge?"
python scripts/query_cli.py -i                       # interactive
python scripts/query_cli.py "..." --show-context     # print the full excerpts
python scripts/query_cli.py "..." --json             # machine-readable
python scripts/query_cli.py "..." --no-llm           # retrieval only, no model
python scripts/query_cli.py "Rule 14(3)" --contains "Rule 14(3)"

# Tests (no ChromaDB, no model download needed)
python -m pytest
```

`create_deduplication_ignore_list_v2.py` imports `pdfplumber` and `python-docx` optionally: PDFs are skipped if pdfplumber is missing. It is resumable via `docs/deduplication_progress.json` and processes `batch_size` files per batch.

`incremental_ingest.py` also accepts `--full-hash` (re-hash everything instead of trusting size+mtime), `--strict` (exit 2 when files are left blocked, for cron), `--no-ocr`/`--ocr-limit N` (OCR controls), and `--source`/`--manifest`/`--chroma-path` overrides for testing against a scratch corpus.

Three read-only-ish modes, from cheapest to most thorough:

| Mode | Reads | Writes | Use it to |
|------|-------|--------|-----------|
| `--dry-run` | hashes changed files | nothing | see which files are NEW/MODIFIED/MOVED/DELETED |
| `--audit` | + extracts and chunks | nothing | size the OCR/unsupported workload before a full run |
| default | + OCR + embeds | manifest + ChromaDB | actually ingest (the only mode that OCRs) |

## Tests

`tests/test_incremental_ingest.py` covers chunking, extraction, classification, the scanning edge
cases and end-to-end runs; `tests/test_query_cli.py` covers retrieval scoring, citation rendering,
both LLM payloads, citation validation, the extractive fallback and the CLI. The vector store, the
embedding model and the LLM transport are all faked, so the suite needs neither
chromadb/sentence-transformers, a model download, an Ollama server nor an API key — keep it that
way, and add a test with every behaviour change to a script.

```bash
python -m pytest              # everything
python -m pytest -k classify  # one area
python -m pytest -k citation  # the query CLI's citation logic
```

## Config Structure (`config.yaml`)

```yaml
docs_source:
  - "D:/Office PC/D DRIVE/KERC"

file_types:
  # High priority
  - "pdf"
  - "txt"
  - "docx"
  - "doc"
  - "md"
  - "rtf"
  - "xlsx"
  - "xls"
  - "xlsm"
  - "csv"
  # Medium (commented): odt, html, htm, json, xml
  # Low (commented): epub, pptx, ppt

ignore_list_path: "docs/deduplication_ignore_list.json"
progress_path: "docs/deduplication_progress.json"
batch_size: 100
signature_prefix_chars: 300

# Office owner/lock files skipped at scan time (matched on file name)
lock_file_patterns:
  - "~$*"
  - ".~lock.*#"

# Embeddings — changing embedding_model re-embeds the corpus (embedding_fingerprint)
embedding_model: "all-MiniLM-L6-v2"

# Query CLI (scripts/query_cli.py) — reads the same collection
llm_backend: "ollama"          # ollama (local, default) | claude | none
llm_top_k: 6                   # excerpts per question
llm_min_score: 0.0             # cosine similarity floor
llm_max_context_chars: 6000    # cap on excerpt text sent to the model
llm_ollama_url: "http://localhost:11434"
llm_ollama_model: "llama3.2:3b"
llm_claude_url: "https://api.anthropic.com/v1/messages"
llm_claude_model: "claude-sonnet-4-5"   # override with a current model id
llm_claude_api_key_env: "ANTHROPIC_API_KEY"   # read from the environment, never config
```

## Deduplication Logic

- **Signature**: size + page count + first/last 300 chars of text (MD5 → 12 chars)
- **Ignore list**: Keeps shortest path per signature group, ignores rest
- **Progress file**: `docs/deduplication_progress.json` (resume capability, gitignored)
- **Result**: 994 PDFs → 939 unique (55 duplicates: blank pages, cross-folder copies, print vs source)

## Incremental Ingestion

`scripts/incremental_ingest.py` implements the strategy in `docs/incremental_update_strategy.md`.
The manifest (`docs/file_manifest.json`, gitignored) tracks size, mtime, SHA256 `content_hash`,
dedup signature, page count, chunk count and status per file. Per change type:

| Type | Detection | Action |
|------|-----------|--------|
| NEW | path not in manifest | extract → chunk → embed → upsert |
| MODIFIED | same path, different hash | re-extract, delete old chunks, upsert new ones |
| MOVED | same hash, new path | rewrite chunk metadata only — no re-embedding |
| DELETED | path gone from manifest | delete that source's chunks |
| UNCHANGED | same hash, status settled | skipped |

- Statuses: `indexed`, `needs_ocr`, `pending_embedding`, `unsupported`, `empty`, `error`.
  Anything left in a retryable status is picked up automatically on a later run once the
  missing capability exists (install Tesseract/the Kannada pack or export the API key, and the
  OCR pass is enabled the next run). Files that keep failing (unreadable, corrupt) are parked
  after `max_retries` attempts and only surface via `--strict`.
- Edge cases handled explicitly, because each one looks like a mass change if ignored:
  - **unreachable source** (unmounted drive, renamed folder) → its previous entries are kept as-is,
    never treated as DELETED, so a missing `D:` cannot wipe the collection
  - **overlapping/nested sources** → a file reachable through two roots is processed once
  - **Office owner/lock files** (`~$*.docx` from Word/Excel, `.~lock.*#` from LibreOffice) → dropped
    at scan time by `lock_file_patterns` (matched on the file name, before the extension filter), so
    they never enter the manifest as retryable `error` entries or trip `--strict`
  - **unreadable file** (locked, permission denied) → `error` status with the reason; the run continues
  - **zero-byte file** → `empty`; **blank non-PDF** → `empty`; **image-only PDF** → `needs_ocr`
  - **touched but unchanged** (mtime only) → UNCHANGED, chunks untouched
  - **case-only rename** on Windows → MOVED, not NEW + DELETED
  - **rename plus edit** (hash changed, dedup signature identical) → reported as a hint, since only a
    human can confirm the old chunks should go
  - **duplicate content** not covered by the ignore list → reported, because both copies get indexed
  - **stale ignore-list entries** → counted, so the list can be regenerated after big moves
  - **interrupted run** → the manifest is re-saved every `batch_size` changes, so progress survives
  - **manifest from a newer script** (`schema_version`) → refused rather than silently downgraded
- Chunk ids are derived as `sha1("<rel_path>|<content_hash>|<index>")`, so the manifest stores only
  `chunk_count` instead of every id, and chunks are still pruned/updated by metadata filters.
- Chunks never span pages, so citations keep an exact page number; `.txt`/`.md`/`.docx` and
  spreadsheets have no pages and record `page = -1`. Partially scanned files keep a
  `pages_without_text` count, which the OCR pass drives down.
- **OCR pass** (`docs/ocr_pass_design.md`): runs between extraction and chunking, page-level, only
  for pages below `ocr_min_chars_per_page`. `ocr_backend` selects `tesseract` | `novita` | `hybrid`
  | `none`; `hybrid` is Tesseract first with a Novita escalation on pages it read too little from.
  - A page is OCR'd only if no engine can read it as text — never a full-corpus re-run. Recovered
    text keeps its page number, so chunking/citations are unchanged.
  - Recovery is cached (`ocr_cache_path`, keyed by `content_hash` + settings + usable engines), so a
    re-run is free and an interrupted run resumes. `ocr_fingerprint` in the manifest re-processes
    an already-indexed PDF whose `content_hash` cannot change but whose blank pages were never fixed.
  - A missing engine parks files as `needs_ocr` (no attempts burned); an engine error is `error` and
    is retried, then parked after `max_retries`. Nothing is ever cached as "empty" on an engine error,
    so a transient failure is retried rather than becoming permanent.
  - `--ocr-limit N` caps the pages sent to an engine in one run (attempted pages count, including
    reads that come back empty); the remainder is deferred to the next run.
  - `ocrmypdf --skip-text` mirrors only the files that actually have OCR text into `ocr_pdf_path`
    (best-effort; a missing Ghostscript is a note, not a failure).
  - `--dry-run`/`--audit`/`--no-ocr` never build an engine, so they cannot spend money.
- **Embedding guard-rail**: every indexed entry stores an `embedding_fingerprint` (a hash of
  `embedding_model`). Changing the model re-processes all indexed files (`re-embed: the embedding
  model changed`), because the content hash cannot show that the vector space changed. A manifest
  from before the field existed is adopted as embedded with the current model, so upgrading does not
  re-embed the corpus; `SCHEMA_VERSION` is 3. Read `docs/stack_choices.md` before switching models.
- Extraction coverage: `.pdf` (pdfplumber), `.docx` (python-docx), `.txt`/`.md`, spreadsheets
  (`.xlsx`/`.xlsm` via openpyxl, `.xls` via xlrd, `.csv` via stdlib). A missing library or an
  unimplemented format yields `unsupported` with the reason, never a crash — `.doc` and `.rtf` are
  still in that bucket (4 `.doc` files in the corpus).
- Spreadsheets are cited by **sheet name**: each worksheet becomes one page whose name is the chunk
  `section` (there are no page numbers to point at), and rows are capped per sheet by
  `spreadsheet_max_rows` with the drop recorded in the file's `note`.
- Without `sentence-transformers`/`chromadb` the run still extracts, chunks and updates the
  manifest (files land in `pending_embedding`); it never blocks on the heavy dependencies.

## Query CLI

`scripts/query_cli.py` answers questions over the collection `incremental_ingest.py` wrote, with
page-exact citations (design: `docs/query_cli_design.md`).

- Retrieval reuses `chroma_path`/`collection_name`/`embedding_model`, converts Chroma's cosine
  **distance** to a similarity score, and can require an exact substring (`--contains`) for rule
  lookups the embedding ranks poorly.
- Excerpts are numbered `[1]…[k]` in the prompt, the model is told to cite with those markers, and
  the markers are validated after generation — an out-of-range `[n]` is surfaced as a hallucinated
  citation and an answer with no markers is flagged.
- A chunk's citation is the file plus page (`order.pdf, p.12`); page-less formats cite the sheet name
  (`tariff.xlsx, sheet "Tariff 2024"`) or the heading, matching the metadata `incremental_ingest.py`
  writes.
- `llm_backend` is `ollama` (local, default), `claude` (key from `llm_claude_api_key_env`) or `none`;
  if the chosen engine is unreachable the CLI returns the cited excerpts and says why, never a silent
  empty answer. No new dependencies — the HTTP calls use the standard library.
- Exit codes: `0` answered, `1` setup error (missing deps or an empty index), `2` no relevant excerpts.

## Conventions

- **Split commits by task, not by session.** When the working tree holds several unrelated changes,
  commit them separately — one commit per feature, bug fix, gap, refactor or docs change — never one
  large "misc changes" commit. Each commit should be reviewable and revertable on its own, so use
  `git add <paths>` per group instead of `git add -A`, and make sure the code and its changelog entry
  land in the same commit. Commit messages explain the *why*, not the file list.
- **Log every change in `CHANGELOG.md`** — no change is too small to record: new files, script edits, config tweaks, doc fixes, `.gitignore` entries. The changelog entry goes in the *same commit* as the change, never a follow-up. Add the entry under `## [Unreleased]`, or start a new version section (`## [X.Y.Z] - YYYY-MM-DD`) above the previous one, matching the existing Keep a Changelog style (`Added` / `Changed` / `Fixed` / `Documentation` / `Removed`). Also refresh the `Summary Statistics` table at the bottom when counts change.
- **No hardcoded paths or file types** — every script reads `docs_source`, `file_types`, and output paths from `config.yaml`. Add new sources/extensions to the config, not to code.
- **Path handling**: `pathlib.Path`; source paths come from a Windows drive (`D:/Office PC/D DRIVE/KERC`) but scripts must keep working when that path is absent (warn and continue).
- **Deduplication never deletes files** — it only emits an ignore list plus a human-readable report.
- **The ignore list is written Windows-style** by `create_deduplication_ignore_list_v2.py` (backslashes, from `str(relative_to(source))`) and matched against POSIX-style manifest keys — always normalise separators when comparing paths between the two scripts.
- **Cost-sensitive OCR**: prefer pdfplumber text extraction and fall back to OCR only for low-text pages, per `docs/origin_doc.md`.
- **Dependencies live in `requirements.txt`** — nothing is installed globally or listed only in prose. Add new deps to the active block; park not-yet-used ones in the commented planned block.
- **Docs over code comments**: design decisions and analyses go in `docs/*.md`; update the relevant doc when a decision changes.
- Shell is bash (Git Bash on Windows) — use POSIX commands (`ls`, `mv`, `rm`), not cmd.exe/PowerShell.

## Git

- Remote: https://github.com/Shreyasss91/rulebook
- Branch: `main`
- Force-pushed for clean linear history
- `.gitignore` excludes PDFs, vector DB, temp files

## Next Milestones

| Milestone | Version |
|-----------|---------|
| ~~RAG query CLI with citation support~~ ✅ `scripts/query_cli.py` | v1.0.0 |
| Gradio UI with citations | v1.1.0 |
| OCR pipeline integration (Tesseract + Novita.ai hybrid) | implemented, awaiting the real-corpus run |
| Scheduled auto-ingest via cron | v1.2.0 |