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
    └── incremental_ingest.py                 # manifest-based change detection + indexing
```

Gitignored and generated at runtime: PDFs, `kerch_db/` (ChromaDB), `data/`, `docs/file_manifest.json`.

## Key Files

| File | Purpose |
|------|---------|
| `config.yaml` | Central config: `docs_source` (list), `file_types` (priority tiers), ignore list, progress, batch size |
| `scripts/create_deduplication_ignore_list_v2.py` | Content-based deduplication (size + first/last page hash) → `docs/deduplication_ignore_list.json` |
| `scripts/scan_extensions.py` | Recursive scan of `docs_source` for extension counts |
| `scripts/incremental_ingest.py` | Incremental pipeline: manifest diff → extract → chunk → embed → ChromaDB upsert |
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

# Incremental ingest: report changes without writing anything
python scripts/incremental_ingest.py --dry-run -v

# Incremental ingest: measure what extraction would produce (needs_ocr, empty,
# unsupported counts) without embedding or writing anything
python scripts/incremental_ingest.py --audit

# Incremental ingest: apply them
python scripts/incremental_ingest.py

# Tests (no ChromaDB, no model download needed)
python -m pytest
```

`create_deduplication_ignore_list_v2.py` imports `pdfplumber` and `python-docx` optionally: PDFs are skipped if pdfplumber is missing. It is resumable via `docs/deduplication_progress.json` and processes `batch_size` files per batch.

`incremental_ingest.py` also accepts `--full-hash` (re-hash everything instead of trusting size+mtime), `--strict` (exit 2 when files are left blocked, for cron), and `--source`/`--manifest`/`--chroma-path` overrides for testing against a scratch corpus.

Three read-only-ish modes, from cheapest to most thorough:

| Mode | Reads | Writes | Use it to |
|------|-------|--------|-----------|
| `--dry-run` | hashes changed files | nothing | see which files are NEW/MODIFIED/MOVED/DELETED |
| `--audit` | + extracts and chunks | nothing | size the OCR/unsupported workload before a full run |
| default | + embeds | manifest + ChromaDB | actually ingest |

## Tests

`tests/test_incremental_ingest.py` covers chunking, extraction, classification, the scanning edge
cases and end-to-end runs. The vector store and the embedding model are faked, so the suite needs
neither chromadb/sentence-transformers nor a model download — keep it that way, and add a test with
every behaviour change to `incremental_ingest.py`.

```bash
python -m pytest              # everything
python -m pytest -k classify  # one area
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
  missing capability exists (flip `OCR_AVAILABLE` in the script when OCR lands). Files that keep
  failing (unreadable, corrupt) are parked after `max_retries` attempts and only surface via `--strict`.
- Edge cases handled explicitly, because each one looks like a mass change if ignored:
  - **unreachable source** (unmounted drive, renamed folder) → its previous entries are kept as-is,
    never treated as DELETED, so a missing `D:` cannot wipe the collection
  - **overlapping/nested sources** → a file reachable through two roots is processed once
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
- Chunks never span pages, so citations keep an exact page number; `.txt`/`.md`/`.docx` have
  no pages and record `page = -1`. Partially scanned files keep a `pages_without_text` count for
  the future OCR pass.
- Without `sentence-transformers`/`chromadb` the run still extracts, chunks and updates the
  manifest (files land in `pending_embedding`); it never blocks on the heavy dependencies.

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
| RAG query CLI with citation support | v1.0.0 |
| Gradio UI with citations | v1.1.0 |
| OCR pipeline integration (Tesseract + Novita.ai hybrid) | — |
| Scheduled auto-ingest via cron | v1.2.0 |