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
│   └── incremental_update_strategy.md       # Manifest-based incremental pipeline design
└── scripts/
    ├── scan_extensions.py
    ├── create_deduplication_ignore_list_v2.py
    └── (planned) incremental_ingest.py
```

Gitignored and generated at runtime: PDFs, `kerch_db/` (ChromaDB), `data/`, `docs/file_manifest.json` (planned).

## Key Files

| File | Purpose |
|------|---------|
| `config.yaml` | Central config: `docs_source` (list), `file_types` (priority tiers), ignore list, progress, batch size |
| `scripts/create_deduplication_ignore_list_v2.py` | Content-based deduplication (size + first/last page hash) → `docs/deduplication_ignore_list.json` |
| `scripts/scan_extensions.py` | Recursive scan of `docs_source` for extension counts |
| `docs/origin_doc.md` | Complete architecture, OCR comparison, hardware assessment, Option A vs B |
| `docs/incremental_update_strategy.md` | Manifest-based incremental pipeline design |
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
```

`create_deduplication_ignore_list_v2.py` imports `pdfplumber` and `python-docx` optionally: PDFs are skipped if pdfplumber is missing. It is resumable via `docs/deduplication_progress.json` and processes `batch_size` files per batch.

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
```

## Deduplication Logic

- **Signature**: size + page count + first/last 300 chars of text (MD5 → 12 chars)
- **Ignore list**: Keeps shortest path per signature group, ignores rest
- **Progress file**: `docs/deduplication_progress.json` (resume capability, gitignored)
- **Result**: 994 PDFs → 939 unique (55 duplicates: blank pages, cross-folder copies, print vs source)

## Incremental Update Strategy (Planned)

Manifest-based (`docs/file_manifest.json`) tracking:
- `content_hash` (SHA256) for change detection
- Change types: NEW, MODIFIED, MOVED (same hash, diff path), DELETED, UNCHANGED
- ChromaDB ops: `upsert` (NEW/MODIFIED), `delete` (DELETED), `update` metadata (MOVED)

## Conventions

- **Log every change in `CHANGELOG.md`** — no change is too small to record: new files, script edits, config tweaks, doc fixes, `.gitignore` entries. The changelog entry goes in the *same commit* as the change, never a follow-up. Add the entry under `## [Unreleased]`, or start a new version section (`## [X.Y.Z] - YYYY-MM-DD`) above the previous one, matching the existing Keep a Changelog style (`Added` / `Changed` / `Fixed` / `Documentation` / `Removed`). Also refresh the `Summary Statistics` table at the bottom when counts change.
- **No hardcoded paths or file types** — every script reads `docs_source`, `file_types`, and output paths from `config.yaml`. Add new sources/extensions to the config, not to code.
- **Path handling**: `pathlib.Path`; source paths come from a Windows drive (`D:/Office PC/D DRIVE/KERC`) but scripts must keep working when that path is absent (warn and continue).
- **Deduplication never deletes files** — it only emits an ignore list plus a human-readable report.
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
| `scripts/incremental_ingest.py` — manifest-based change detection | v0.9.0 |
| RAG query CLI with citation support | v1.0.0 |
| Gradio UI with citations | v1.1.0 |
| OCR pipeline integration (Tesseract + Novita.ai hybrid) | — |
| Scheduled auto-ingest via cron | v1.2.0 |