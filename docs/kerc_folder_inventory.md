# KERC Folder Inventory — Page Count Analysis

**Generated:** 2026-09-25  
**Source:** `D:\Office PC\D DRIVE\KERC`  
**Tool:** `pdfplumber` page count scan

---

## Summary

| Metric | Value |
|--------|-------|
| **Total PDFs** | 994 |
| **Total Pages** | 25,453 |
| **Est. Unique Content** | ~10,000–12,000 pages (after deduplication) |

---

## Breakdown by Category

| Category | PDFs | Pages | Notes |
|----------|------|-------|-------|
| **OMBUDSMAN ORDERS** | ~350 | ~6,500 | Many duplicates (RAW + MERGED versions) |
| **PRINT MERGED** | 14 | ~3,300 | Large merged manuals (500+ pages each) |
| **WBESCL TECHNICAL SPECS** | ~150 | ~3,000 | Equipment specifications |
| **KERC regulations** | ~50 | ~1,500 | Core KERC rules, tariff orders, codes |
| **BESR** | 12 | ~1,200 | Service regulations |
| **CEA/CEA Regulations** | ~20 | ~1,000 | Central electricity authority |
| **EXECUTIVE HIGHER** | ~30 | ~1,000 | Exam materials |
| **ELECTRICAL INSPECTORATE** | ~40 | ~800 | Safety, licensing, lift acts |
| **KPTCL ESCOMS** | ~30 | ~800 | Standards, SR manuals |
| **GOK/CHECK/others** | ~100 | ~1,500 | Various |

---

## Largest Individual PDFs (>200 pages)

| PDF | Pages | Category |
|-----|-------|----------|
| `PRINT MERGED\MAIN MERGED 1.pdf` | 532 | Merged manual |
| `PRINT MERGED\MAIN_MERGED_2.pdf` | 345 | Merged manual |
| `PRINT MERGED\BESR MERGED.pdf` | 345 | Merged manual |
| `CEA_CENTRALELECTRICITYAUTHORITY\regulation_elec_safety.pdf` | 367 | CEA |
| `PRINT MERGED\ACCOUNTS MANUAL VOLUME-3.COMMERCIAL ACCOUTING SYSTEM(pdf).pdf` | 367 | Merged manual |
| `OMBUDSMAN ORDERS\ALL OMBUDSMAN ORDERS MERGED\OMBUDSMAN ORDERS 2019.pdf` | 463 | Ombudsman |
| `OMBUDSMAN ORDERS\ALL OMBUDSMAN ORDERS MERGED\OMBUDSMAN ORDERS 2020.pdf` | 432 | Ombudsman |
| `PRINT MERGED\ACCOUNTS MANUAL_VOLUME_2_PARTA_MERGED.pdf` | 394 | Merged manual |
| `PRINT MERGED\ACCOUNTS MANUAL_VOLUME_2_PARTB_MERGED.pdf` | 331 | Merged manual |
| `KERC\Tariff Orders Mescom\MERGED.pdf` | 251 | KERC Tariff |
| `BESR\cas.pdf` | 350 | BESR |

---

## Deduplication Observations

1. **OMBUDSMAN ORDERS** — RAW files + MERGED yearly compilations + ALL YEARS MERGED = 3x duplication
2. **DICTIONARY_BESCOM** — Appears in root, `KPTCL ESCOMS`, `EXECUTIVE HIGHER`
3. **DICTIONARY_Web Padakosha** — Appears in root, `KPTCL ESCOMS`
4. **COS_MERGED** — Appears in root, `PRINT MERGED`
5. **ROE_MERGED** — Appears in root, `PRINT MERGED`
6. **BESCOM Drawings** — 60+ single-page DWG files (likely vector, not scanned)

---

## Cost Estimates (Novita.ai DeepSeek OCR 2 @ $0.03/M tokens)

| Scenario | Pages | Est. Cost |
|----------|-------|-----------|
| **All 25,453 pages** | 25,453 | **$0.76–2.30** |
| **Deduplicated (unique content)** | ~12,000 | **$0.36–1.10** |
| **Core KERC only (regulations, tariff, codes)** | ~3,000 | **$0.09–0.27** |

---

## Scanned vs Digital-Born Assessment

| Content Type | Likely Scanned | Likely Digital-Born |
|--------------|----------------|---------------------|
| OMBUDSMAN RAW orders | ✅ Yes | |
| BESCOM Drawings (DWG-*) | ✅ Yes (1-page each) | |
| PRINT MERGED manuals | | ✅ Yes |
| KERC Regulations | | ✅ Yes |
| CEA Regulations | | ✅ Yes |
| WBESCL Tech Specs | | ✅ Yes |
| EXECUTIVE HIGHER papers | | ✅ Yes |

**Estimated scanned pages:** ~2,000–3,000 (mostly Ombudsman RAW + BESCOM drawings)

---

## Measured Ingest Audit (2026-09-25)

First measurement of the real corpus, from `python scripts/incremental_ingest.py --audit`
(read-only: it extracts and chunks but writes nothing). These are counts, not estimates.

| Metric | Value |
|--------|-------|
| Files scanned (configured types) | 1,036 |
| Skipped via the dedup ignore list | 55 |
| **Indexable as-is** | **819 files → 19,098 pages → 37,118 chunks** |
| Needs OCR (no text layer) | 143 files |
| Unsupported format | 10 files (4 `.doc`; 6 spreadsheets, since fixed) |
| Empty | 1 file (`EA Doc3.docx`) |
| Unreadable | 8 files — all Word lock files (`~$VER PAGE.docx`) |
| Pages with no text inside *indexable* files | 1,246 pages across 142 files (7% of extracted pages) |
| Duplicate content outside the ignore list | none |
| Stale ignore-list entries | none |

### What this changes

- **The OCR workload is smaller than the earlier guess in both directions.** Files that cannot be
  indexed at all: 143, holding roughly 6,400 pages (25,453 total − 19,098 extracted). At Novita.ai's
  $0.00003–0.0001/page that is **$0.19–0.64** for the whole remaining OCR job, so there is no reason to
  triage folders by hand.
- **The bigger surprise is inside the files that do index:** 1,246 pages (7%) of already-indexable
  documents have no text layer, across 142 files. Those are mixed print/scan manuals — they will be
  silently unsearchable unless the OCR pass fills the gaps. This is the argument for page-level OCR
  rather than file-level (the script already records `pages_without_text` per file).
- **The scanned/digital split above is off.** `ELECTRICAL INSPECTORATE`, `EXECUTIVE HIGHER` and
  `KPTCL ESCOMS` hold as many image-only files as `OMBUDSMAN ORDERS`; the earlier table assumed only
  Ombudsman RAW and BESCOM drawings were scans.
- **Embedding load is 37,118 chunks** for the first pass, before any OCR text is added.
- Spreadsheet handling was fixed during this audit (`scripts/incremental_ingest.py`); the 4 `.doc`
  files still need antiword/LibreOffice, and `~$*.docx` Word lock files should ideally come out of the
  corpus rather than being retried as errors.

### needs_ocr by top-level folder

| Folder | Files |
|--------|-------|
| CHECK and PRINT | 29 |
| ELECTRICAL INSPECTORATE | 20 |
| EXECUTIVE HIGHER | 18 |
| KPTCL ESCOMS | 18 |
| OMBUDSMAN ORDERS | 18 |
| KERC | 13 |
| remaining folders | 27 |

Note: an audit run hashes every file and takes about 75 minutes on the i5/8 GB machine this corpus
lives on (~25 min hashing and PDF signatures, ~50 min text extraction), which is why the results are
recorded here rather than regenerated casually.

---

## Recommended Processing Order

1. **Deduplicate** — Remove exact duplicates by content hash
2. **Prioritize core KERC** — Regulations, Tariff Orders, Grid/Distribution Codes, Supply Code, COS, SOP
3. **OCR scanned subset** — Ombudsman RAW, BESCOM drawings
4. **Skip/Defer** — Duplicate merged manuals, exam papers, contractor forms

---

## Next Steps

- [x] Write deduplication script (SHA-256 of first/last page text + file size)
- [x] Create prioritized ingestion list
- [ ] Test OCR on sample scanned pages — start with the 143 `needs_ocr` files and the 1,246 blank
      pages inside indexed files
- [ ] Build RAG index with citations — 819 files / 37,118 chunks are ready to embed today

---

*Run `python count_pages.py` to regenerate this inventory.*