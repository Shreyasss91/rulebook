# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [Unreleased]

### Planned
- Build RAG query interface (CLI + Gradio UI) with citation support
- Add OCR pipeline integration (Tesseract + Novita.ai DeepSeek OCR 2 hybrid)
- Add spreadsheet extraction (xlsx/xls/xlsm/csv) so those files stop reporting `unsupported`
- Signature-based move detection for renames that also edit content (now a hint only)

---

## [0.10.0] - 2026-09-25

### Fixed
- **Ignore list never matched on Windows** — `create_deduplication_ignore_list_v2.py` writes
  `str(relative_to(source))`, which contains backslashes, while the manifest uses POSIX keys. Every
  entry in `docs/deduplication_ignore_list.json` was silently ignored, so the 55 deduplicated files
  would have been indexed a second time. Separators are normalised on load, absolute entries match by
  normalised absolute path, and both cases now have tests
- **A missing source wiped the collection** — if `docs_source` was unreachable (drive unplugged,
  folder renamed), every file in it was reported DELETED and its chunks dropped. Entries of an
  unreachable root are now kept as-is and the situation is warned about
- **Heading fragments were indexed as chunks** — a heading line could become a 10-character chunk of
  its own. A short buffer now stays attached to the content that follows it

### Added
- **Chunking robustness**: `chunk_text` keeps runts merged, so no text is dropped just because it is
  short, and every chunk carries its section heading
- **Edge-case handling in `incremental_ingest.py`**:
  - unreadable/locked files → `error` status with the reason instead of crashing the run
  - zero-byte and blank files → `empty`; only PDFs are treated as OCR candidates
  - partially scanned PDFs → `pages_without_text` recorded for the future OCR pass
  - overlapping/nested `docs_source` entries → scanned once, with a warning
  - duplicate content outside the ignore list → reported, since each copy is indexed
  - stale ignore-list entries → counted so the list can be regenerated
  - rename-plus-edit (hash changed, dedup signature identical) → reported as a hint
  - `max_retries` (default 3) parks repeatedly failing files; `--strict` still flags them
  - the manifest is re-saved every `batch_size` changes, so an interrupted run keeps its progress
  - a manifest with a newer `schema_version` is refused instead of silently re-processed
- `config.yaml`: `max_retries` setting

### Documentation
- `CLAUDE.md` documents the edge cases and the ignore-list separator pitfall
- `docs/incremental_update_strategy.md` lists the situations beyond the original four scenarios

---

## [0.9.0] - 2026-09-25

### Added
- **Incremental Ingestion** (`scripts/incremental_ingest.py`) — Option A from `docs/incremental_update_strategy.md`
  - Manifest (`docs/file_manifest.json`, gitignored) records size, mtime, SHA256 `content_hash`, dedup signature, page count, chunk count and status per file
  - Change classification: NEW, MODIFIED, MOVED (same hash at a new path), DELETED, UNCHANGED
  - Vector DB operations: upsert for NEW/MODIFIED, metadata re-label for MOVED (no re-embedding),
    delete by source for DELETED
  - Legal-aware chunking keeps rule/section headings attached to their chunk; chunks never span
    pages, so citations carry an exact page number
  - Chunk ids derived as `sha1("<rel_path>|<content_hash>|<index>")`, so the manifest stores a count
    rather than every id
  - Degrades gracefully: without sentence-transformers/chromadb it still extracts, chunks and
    updates the manifest, marking files `pending_embedding` for a later run
  - Statuses: `indexed`, `needs_ocr`, `pending_embedding`, `unsupported`, `empty`, `error`;
    retryable ones are re-processed automatically once the blocking capability exists
  - CLI: `--dry-run`, `--full-hash`, `--strict` (exit 2 for cron), `--source`/`--manifest`/`--chroma-path` overrides
  - Size+mtime fast path avoids re-hashing unchanged files on every run

### Changed
- `config.yaml` gained incremental ingest settings: `manifest_path`, `track_moves`,
  `track_deletions`, chunking (`chunk_size`, `chunk_overlap`, `min_chunk_chars`),
  embeddings/vector store (`embedding_model`, `embedding_batch_size`, `chroma_path`,
  `collection_name`) and `ocr_min_chars_per_page`
- `requirements.txt`: sentence-transformers/chromadb relabelled from "planned" to optional, since
  the script now uses them when present
- `docs/incremental_update_strategy.md` records the implementation status and where the built
  script deviates from the original sketch

### Verified
- Scratch corpus run covering all four documented scenarios: new file, new subfolder, edit, move,
  delete — plus idempotent rerun, dry run (writes nothing) and a run with embeddings/chromadb
  unavailable followed by a successful retry

---

## [0.8.0] - 2026-09-25

### Added
- **Agent Onboarding Guide** (`CLAUDE.md`)
  - First committed project guide: architecture, repository layout, key files, commands, conventions
  - Records that scripts resolve `config.yaml` relative to the project root
  - Records that `pyyaml` is required while `pdfplumber`/`python-docx` are lazily imported (matching file types degrade to a size+path signature instead of failing)
  - Adds the changelog rule: every change goes in `CHANGELOG.md` in the same commit

- **Dependency Manifest** (`requirements.txt`)
  - Active block: `pyyaml`, `pdfplumber`, `python-docx`
  - Commented planned blocks, mirroring the priority tiers in `config.yaml`:
    - RAG pipeline: `sentence-transformers`, `chromadb`
    - OCR wrappers: `pytesseract`, `ocrmypdf` (require Tesseract/Ghostscript binaries)

### Changed
- `.gitignore` now excludes `docs/file_manifest.json`, the generated state file the
  planned incremental pipeline writes — `docs/incremental_update_strategy.md`
  described it as ignored, but the entry was missing
- `CLAUDE.md` install instructions now point at `requirements.txt`

### Documentation
- `CHANGELOG.md` is maintained per-commit rather than backfilled in batches

---

## [0.7.0] - 2026-09-25

### Added
- **Incremental Update Strategy** (`docs/incremental_update_strategy.md`)
  - Problem statement: handling edits, additions, new subfolders, moves after initial indexing
  - Manifest-based approach: `docs/file_manifest.json` tracking path, hash, mtime, chunks
  - Change classification: NEW, MODIFIED, MOVED, DELETED, UNCHANGED
  - Per-component incremental strategies (dedup, OCR, chunking, embeddings, vector DB)
  - ChromaDB operations for each change type (upsert, delete, update metadata)
  - Three implementation options with recommendation (custom manifest for control)

### Documentation
- Complete incremental strategy document with code examples

---

## [0.6.0] - 2026-09-25

### Added
- **Configuration System** (`config.yaml`)
  - YAML-based config (replaced JSON)
  - Multi-source support: `docs_source` as list of folders
  - Multi-filetype support with priority tiers:
    - High: pdf, txt, docx, doc, md, rtf, xlsx, xls, xlsm, csv
    - Medium (commented): odt, html, htm, json, xml
    - Low (commented): epub, pptx, ppt

- **Extension Scanner** (`scripts/scan_extensions.py`)
  - Reads `docs_source` from config.yaml
  - Recursive scan with extension counting
  - Found: 994 pdf, 26 docx, 6 txt, 4 doc, 3 xlsx, 2 xls, 1 xlsm, 2 rar, 2 zip, 1 db

### Changed
- Refactored deduplication script to use `config.yaml`
- Removed hardcoded paths and file types
- Deleted legacy `config.json`

---

## [0.5.0] - 2026-09-25

### Added
- **Deduplication System** (`scripts/create_deduplication_ignore_list_v2.py`)
  - Content-based signature (size + first/last page text hash)
  - Resume capability via progress file
  - Batch processing (100 files/batch)
  - Outputs: `docs/deduplication_ignore_list.json` + `docs/deduplication_report.md`

- **Results**
  - 994 PDFs scanned → 55 duplicates found → 939 unique kept
  - Duplicates: blank pages across years, cross-folder copies, print vs source
  - Ignore list used by ingestion pipeline to skip duplicates

### Documentation
- `docs/deduplication_report.md` — Detailed duplicate groups
- `docs/deduplication_ignore_list.json` — Machine-readable ignore list

### Fixed
- Added `deduplication_progress.json` to `.gitignore` (temp file)
- Removed superseded v1 script

---

## [0.4.0] - 2026-09-25

### Added
- **KERC Folder Inventory** (`docs/kerc_folder_inventory.md`)
  - Complete scan: 994 PDFs, 25,453 pages
  - Breakdown by category (OMBUDSMAN, PRINT MERGED, WBESCL, KERC, BESR, CEA, etc.)
  - Largest individual PDFs identified (500+ page merged manuals)
  - Deduplication analysis: ~50% duplicates, ~12,000 unique pages

- **Cost Projections**
  - All pages via Novita.ai: $0.76–2.30
  - Deduplicated: $0.36–1.10
  - Core KERC only: $0.09–0.27

### Documentation
- `docs/kerc_folder_inventory.md` with full breakdown

---

## [0.3.0] - 2026-09-25

### Added
- **Novita.ai DeepSeek OCR 2 Pricing Analysis** (`docs/origin_doc.md`)
  - Actual pricing: $0.03 / 1M tokens (input + output)
  - Cost per page: ~$0.00003–0.0001 (3–10 cents per 1,000 pages)
  - 500–1,000 pages = $0.015–0.10 total (vs previous $0.50–5.00 estimate)
  - Hardware constraints now irrelevant for API usage

### Changed
- Updated cost estimates and recommendations in `docs/origin_doc.md`
- API-based OCR now recommended as viable for full corpus

---

## [0.2.0] - 2026-09-25

### Added
- **Tesseract vs DeepSeek OCR Comparison** (`docs/origin_doc.md`)
  - Detailed accuracy comparison by document type (clean text, tables, handwriting, multi-column, formulas)
  - KERC-specific assessment: government orders, tariff tables, Kannada/English mixed, rule numbering
  - Hybrid approach recommendation: 90% Tesseract + 10% DeepSeek OCR API

- **DeepSeek OCR Local Inference Hardware Assessment** (`docs/origin_doc.md`)
  - Tested on: Intel i5-13500T, 8 GB RAM, Intel UHD 770 (2 GB VRAM)
  - 1.3B model: Possible on CPU (~5-10 sec/page), but lower quality than Tesseract
  - 7B+ models: Not feasible (VRAM/RAM insufficient)
  - Recommendation: Use API instead of local inference

### Documentation
- Updated `docs/origin_doc.md` with OCR comparison and hardware assessment

---

## [0.1.0] - 2026-09-25

### Added
- **Initial RAG Architecture** (`docs/origin_doc.md`)
  - Local-first RAG pipeline design for KERC regulatory documents
  - Architecture: PDFs → Text Extraction → OCR Fallback → Chunking → Embeddings → Vector DB → LLM Query → Cited Answers
  - Stack: ChromaDB, sentence-transformers, Ollama/Claude API, pdfplumber, LangChain/LlamaIndex

- **OCR Engine Integration**
  - Tesseract via `pytesseract` and `ocrmypdf` for searchable PDF layer
  - Fallback strategy: pdfplumber first, OCR only for low-text pages
  - Dependencies documented for Windows installation

- **Option A vs Option B Comparison**
  - Option A: Custom Python prototype (full control, legal-aware chunking, custom citations)
  - Option B: Existing tools (AnythingLLM, Kotaemon, PrivateGPT)
  - Decision matrix for choosing based on priorities

### Documentation
- `docs/origin_doc.md` — Complete architecture and comparison document

---

## Repository Setup

### Git
- Repository: https://github.com/Shreyasss91/rulebook
- Initial commit: a261cd3 (2026-09-25)
- Main branch with linear history (force-pushed for clean history)

### Project Structure
```
rule_books/
├── config.yaml                    # Central configuration
├── requirements.txt               # Python dependencies (planned ones commented)
├── .gitignore                     # Excludes PDFs, vector DB, temp files
├── CHANGELOG.md                   # This file
├── CLAUDE.md                      # Onboarding guide for coding agents
├── docs/
│   ├── origin_doc.md              # Main architecture & decisions
│   ├── kerc_folder_inventory.md   # Corpus analysis
│   ├── deduplication_report.md    # Duplicate analysis
│   ├── deduplication_ignore_list.json
│   ├── incremental_update_strategy.md
│   └── file_manifest.json         # (gitignored, generated at runtime)
├── scripts/
│   ├── create_deduplication_ignore_list_v2.py
│   ├── scan_extensions.py
│   └── incremental_ingest.py      # Manifest diff + extract/chunk/embed/upsert
└── kerch_db/                      # ChromaDB vector store (gitignored, created on first run)
```

---

## Summary Statistics (as of 2026-09-25)

| Metric | Value |
|--------|-------|
| **Commits** | 18 |
| **Documents** | 6 markdown files (4 in `docs/`, 2 at root) |
| **Scripts** | 3 Python scripts |
| **Config** | 1 YAML file + 1 requirements file |
| **Corpus** | 994 PDFs, 25,453 pages (939 unique after dedup) |
| **Est. OCR Cost** | $0.36–1.10 (deduplicated, via Novita.ai) |
| **Unique File Types** | 10 extensions found |

---

## Next Milestones

| Milestone | Target |
|-----------|--------|
| RAG query CLI | v1.0.0 |
| Gradio UI with citations | v1.1.0 |
| Scheduled auto-ingest | v1.2.0 |