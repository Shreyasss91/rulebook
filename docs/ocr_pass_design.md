# OCR Pass — Design

Status: **design, not implemented**. This document is the plan for the deferred
`add ocr pass` task in `docs/deferred.txt` and the "OCR pipeline integration" milestone in
`CLAUDE.md`. Nothing here changes the pipeline yet.

Related reading: `docs/origin_doc.md` (engine comparison, pricing, hardware), `docs/kerc_folder_inventory.md`
(the measured audit), `docs/incremental_update_strategy.md` (manifest model this builds on).

---

## 1. Problem

The first `--audit` measured the corpus (`docs/kerc_folder_inventory.md`):

| Metric | Value |
|--------|-------|
| Files that cannot be indexed at all | **143** (image-only PDFs) — roughly 6,400 pages |
| Pages with no text layer *inside* otherwise-indexable files | **1,246 pages (7%)** across 142 files |
| Estimated API cost for the whole job | **$0.19–0.64** (Novita.ai DeepSeek OCR 2) |

Today those pages are invisible to retrieval. The 143 image-only files are parked as `needs_ocr`;
the 1,246 blank pages inside mixed print/scan manuals are worse, because the file *is* indexed —
a citation can point at a page whose content was never extracted. Today the script counts them
(`pages_without_text`) and moves on.

The goal: recover that text, attribute it to the right page, and fold it into the existing
incremental pipeline without a full re-ingest, without changing the file hashes that drive change
detection, and without silently spending money on a `--dry-run`/`--audit`.

## 2. Goals / non-goals

**Goals**

- Fill the text layer for `needs_ocr` files and blank pages inside indexed files.
- Page-level, not file-level: OCR exactly the pages with no usable text, leave the rest alone.
- Work incrementally and be re-runnable: OCR text is cached so a re-run costs nothing and an
  interrupted run resumes.
- Never break the guarantees already in place — page-exact citations, `content_hash` change
  detection, the dedup ignore list, `--strict` semantics, and the dependency-free test suite.
- Keep cost bounded and visible: report candidate pages and spend before paying, and cap it.

**Non-goals**

- Re-OCR pages that already have text (no full-corpus re-run).
- A different chunking/embedding path: OCR text enters at exactly the point pdfplumber's text does,
  so chunking, ids and citations are unchanged.
- A UI. The query CLI/Gradio are separate milestones.
- Training or hosting a model (local DeepSeek inference was already ruled out in `docs/origin_doc.md`).

## 3. Where it plugs in

```text
scan_sources ──► classify ──► process_new_or_modified
                                 │
                                 ├─ extract_pages(path, config)          # pdfplumber
                                 │     └─► NEW: ocr_missing_pages(...)   # only blank/low pages
                                 │           └─► ocrmypdf --skip-text    # searchable PDF artefact (best-effort)
                                 ├─ build_chunks(pages)                  # unchanged
                                 ├─ index_chunks(...)                    # unchanged
                                 └─ manifest fields                      # + ocr_* fields
```

`extract_pages()` returns `[{"page": int|None, "text": str, "label": str|None}]`. OCR is a
post-processing step on that list: for each page whose stripped text is below
`ocr_min_chars_per_page`, run the engine and replace `page["text"]`. Everything downstream
(`has_text_layer`, `build_chunks`, `make_chunk_id`, `chunk_metadata.page`) keeps working, because
the page number is preserved. This is the only insertion point — no new pipeline stage.

## 4. Engine abstraction

`docs/origin_doc.md` recommends Tesseract for the bulk (free, local) and Novita.ai DeepSeek OCR 2
for hard pages (tables, Kannada/English mix, structured output). That maps to one small interface
with two implementations:

```python
class OcrEngine:                       # scripts/ocr.py or a section of incremental_ingest.py
    name: str
    @staticmethod
    def available(config: dict) -> bool
    def ocr_page(self, image, config: dict) -> str      # one rendered page -> text
```

| Engine | Backend | Cost | Use |
|--------|---------|------|-----|
| `TesseractEngine` | `pytesseract` + Tesseract binary | $0 | default, bulk, clean scans |
| `NovitaEngine` | HTTP API (DeepSeek OCR 2) | ~$0.00003–0.0001/page | escalation, tables, Kannada, low-confidence pages |

`ocr_backend` in `config.yaml` selects the strategy:

- `tesseract` — local only.
- `novita` — API only.
- `hybrid` — Tesseract first; escalate a page to Novita when Tesseract returns less than
  `ocr_escalate_below_chars` characters or the engine reports low confidence. **This is the default**
  (`ocr_backend: hybrid`), matching `docs/origin_doc.md`.

Rendering: pdfplumber's `page.to_image(resolution=ocr_dpi)` (Pillow/pypdfium2) avoids adding poppler
as a system dependency, so the new pieces are the OCR wrapper + Tesseract binary, and — for the
searchable-PDF artefact below — `ocrmypdf` + Ghostscript. Rendering happens at
`ocr_dpi` (default 300, as `docs/origin_doc.md` advises for scanned government orders).

Language: `ocr_languages` (default `eng+kan`) is required **strictly**: KERC orders mix English and
Kannada, and OCR'ing them as English-only would silently garble the Kannada sections. An engine whose
language data is not installed reports *not available* (checked once at start-up via Tesseract's
language list, and by the API's model for Novita), so affected files stay `needs_ocr` and are picked
up once the pack is installed — no English-only fallback, and no wasted attempt.

## 5. Caching, artefacts and idempotency

OCR is the slow/expensive step, so its output is cached rather than recomputed:

- Cache key: `sha256(<content_hash>|<page>|<backend>|<ocr_dpi>|<languages>)` → text.
- Store under `ocr_cache_path` (default `data/ocr_cache/`), one JSONL/JSON file per source file,
  **gitignored** (`data/` already is), so nothing large lands in the repo.
- Same `content_hash` + same OCR settings ⇒ cache hit, no engine call. This is what makes a re-run
  after a crash free, and what makes `--full-hash` runs cheap.
- The rendered image is never stored; only the text. The cache is the source of truth for chunking,
  so retrieval does not depend on the artefact below and works even when `ocrmypdf` is absent.

### Searchable PDF artefacts

**Decision: also emit searchable PDFs**, so a human browsing `D:/.../KERC` in a normal PDF reader can
search the scanned orders too. Rules:

- Written to `ocr_pdf_path` (default `data/ocr_pdfs/`), **mirrored by relative path**, and gitignored.
  The corpus itself stays read-only — nothing is ever written into `docs_source`, preserving the
  "deduplication/ingest never modifies files" convention.
- Produced with `ocrmypdf --skip-text --language eng+kan`, whose `--skip-text` leaves pages that
  already have a text layer untouched and OCRs only the missing ones — the same page-level intent as
  the text pass, so the two cannot diverge on which pages are touched.
- **Only files that actually had pages OCR'd get an artefact** (~285 files, not the whole corpus), so
  the disk cost is a fraction of a full mirror rather than a duplicate of all 994 PDFs.
- Regenerated only when the artefact is missing or the source `content_hash` changed. `ocrmypdf` is
  free and local; it is never called for a page the cache already answered.
- Best-effort: if Ghostscript/`ocrmypdf` is missing, the text cache still populates the chunks and
  the run records a note instead of failing — the artefact is a by-product, not the deliverable.

## 6. Change detection: the one correctness trap

OCR text is derived, so the PDF's `content_hash` does not change when the cache fills in. The
current classifier would therefore report `UNCHANGED` and skip a file that already has chunks —
which means the 1,246 blank pages inside *already-indexed* files would never get fixed. This is the
part of the design that has to be explicit.

Approach: add an **OCR revision fingerprint** to the manifest entry.

- Compute `ocr_fingerprint = sha1(f"{backend}|{dpi}|{languages}|{min_chars}")[:12]` from the OCR
  settings only (not the file content).
- Store `entry["ocr_fingerprint"]` next to `entry["pages_without_text"]`/`entry["ocr_pages"]`.
- In `classify()` / `needs_reprocessing()`, re-process a file whose content hash is unchanged when
  **any** of:
  - `status == needs_ocr` and an engine is now available, or
  - the file previously had `pages_without_text > 0` and its stored `ocr_fingerprint` differs from
    the current one (settings changed, or it was indexed before OCR existed).

Pre-OCR entries have no `ocr_fingerprint`, so the first run after this lands treats any indexed file
with `pages_without_text > 0` as due — exactly the 142 files the audit found — and after that they
settle. Bump `SCHEMA_VERSION` to 2 so an old manifest is understood rather than misread (the
existing guard already refuses a *newer* manifest).

Because a re-processed file is a `MODIFIED`, `index_chunks` deletes the file's old chunks by
`source` before upserting, so stale no-text chunks cannot linger. `MOVED` is unaffected: it
relabels metadata only, and no OCR is run.

## 7. Statuses and retry semantics

Reuse the existing status vocabulary rather than inventing a parallel one:

| Situation | Status | Attempts? |
|-----------|--------|-----------|
| OCR ran and text was recovered | `indexed` / `pending_embedding` | reset to 0 |
| All candidate pages still empty after OCR | `needs_ocr` | no (capability gap) |
| Engine not installed / no API key | `needs_ocr`, note says which capability is missing | **no** |
| Engine errored on this file (corrupt, password-protected, timeout) | `error` + reason | yes (ceiling at `max_retries`, then parked for `--strict`) |
| Some pages recovered, some failed | index what was recovered; `needs_ocr` **only** if nothing was | no |

The distinction that matters: **a missing tool is not a bad file**. Today `needs_ocr` is a static
`OCR_AVAILABLE = False`; that becomes `ocr_available(config)` = an engine is configured *and* its
dependency/credentials are present. Until then files stay politely parked and are picked up the run
after the tool is installed, without burning `max_retries` on a condition the file cannot control.

Partial success is recorded, not discarded: `entry["ocr_pages"]` (pages OCR'd), and
`pages_without_text` is written from what is *still* empty after the pass, so a later run — or a
better engine — can finish the job.

## 8. Cost and blast-radius controls

The whole job is pennies, but the safety rules still matter because a mistake wastes a full corpus
run:

- **No spend without intent.** `--audit` and `--dry-run` must never call a paid engine. They keep
  reporting OCR candidates (counts and pages) so the workload can be sized for free. Only a real run
  (`write=True`) OCRs; `--no-ocr` opts a real run out entirely.
- `ocr_max_pages_per_file` caps one runaway document.
- `--ocr-limit N` caps the number of *pages* OCR'd in a single run, for a cheap trial pass over a
  subset; the rest are simply left for the next run.
- `--source` + `--ocr-limit` give a safe first real test on one folder.
- The searchable-PDF artefact is free and local (`ocrmypdf`/Tesseract) and covers only files that
  were OCR'd, so it adds no API cost.
- The summary prints pages OCR'd, cache hits, engine calls, and estimated spend before/after, so the
  number is visible even when small.

## 9. Config additions (`config.yaml`)

```yaml
# OCR pass (docs/ocr_pass_design.md)
ocr_backend: "hybrid"              # tesseract | novita | hybrid  (default: hybrid)
ocr_dpi: 300                       # page render resolution for scanned orders
ocr_languages: "eng+kan"           # strict - no English-only fallback
ocr_cache_path: "data/ocr_cache"   # gitignored; keyed by content_hash + settings
ocr_pdf_path: "data/ocr_pdfs"      # gitignored; searchable PDFs for the files that were OCR'd
ocr_write_searchable_pdfs: true    # ocrmypdf --skip-text artefact, best-effort
ocr_escalate_below_chars: 200      # hybrid: escalate a Tesseract page below this
ocr_max_pages_per_file: 500        # runaway-document cap
# ocr_min_chars_per_page: 50       # already exists - the page-level trigger
```

No new hardcoded paths or types: `ocr_cache_path` and the engine choice come from config, matching
the project convention. The API key is read from the environment (`NOVITA_API_KEY`), never from
`config.yaml`.

## 10. Testing (stays dependency-free)

The suite already fakes chromadb/sentence-transformers; OCR is faked the same way — a
`FakeOcrEngine` that returns canned text and counts calls. No Tesseract binary, no network, no API
key.

- page-level trigger: a mixed PDF OCRs only its blank pages; a page with text is untouched
- cache: second run makes zero engine calls; a changed `content_hash` misses the cache
- fingerprint: an indexed file with `pages_without_text > 0` and no `ocr_fingerprint` is re-processed
  once, then settles to `UNCHANGED`
- capability gating: no engine configured → `needs_ocr`, `attempts` unchanged; `--audit`/`--dry-run`
  make zero engine calls even with an engine configured
- failures: engine raising (missing binary vs corrupt file) maps to `needs_ocr` vs `error` with the
  right attempt handling and eventual parking under `--strict`
- partial success: some pages recovered → indexed, remainder recorded in `pages_without_text`
- limits: `ocr_max_pages_per_file` and `--ocr-limit` cap engine calls and are reported
- artefact: a file that had pages OCR'd gets one faked `ocrmypdf --skip-text` call into `ocr_pdf_path`,
  a file with a full text layer gets none, and a missing `ocrmypdf`/Ghostscript records a note instead
  of failing the run
- citations: a recovered page keeps its page number in chunk metadata

## 11. Rollout

1. **Spike (no code committed).** Run Tesseract on ~20 pages from `OMBUDSMAN ORDERS`/`KPTCL ESCOMS`
   and compare against Novita on the same pages, to confirm quality assumptions in
   `docs/origin_doc.md` on the real scans. Cheap, and it can change the default backend.
2. **Phase 1 — Tesseract engine + page-level pass + cache + fingerprint + tests.** The core
   behaviour above, defaulting to `tesseract` so nothing paid runs by accident.
3. **Phase 2 — `hybrid` escalation to Novita**, gated behind the API key being present.
4. **Phase 3 — run it.** `--source` one folder with `--ocr-limit`, then the full drive. Requires the
   external `D:` drive mounted and Tesseract installed; this is a deliberate manual run, not
   something the script does on its own. Update `docs/kerc_folder_inventory.md` with the real
   recovered-page counts.
5. Only after that is `needs_ocr` expected to drain to near zero, and the milestone closed.

External prerequisites for step 4: the `D:` drive mounted, the Tesseract binary with the **Kannada**
traineddata (`kan`) installed, Ghostscript (for `ocrmypdf` artefacts), and `NOVITA_API_KEY` exported
for the `hybrid` escalation.

## 12. Settled decisions

Resolved 2026-09-26, before implementation:

1. **Default backend: `hybrid`.** Tesseract carries the bulk for free; low-confidence pages escalate
   to Novita DeepSeek OCR 2. `NOVITA_API_KEY` is required for escalation; if the key is absent the
   run degrades to Tesseract-only with a note rather than stalling. Whole-job cost stays ~$0.19–0.64.
2. **OCR text lives in both a page cache and searchable PDFs.** The cache
   (`data/ocr_cache/`) is the retrieval source of truth; `ocrmypdf --skip-text` mirrors only the
   affected files into `data/ocr_pdfs/` for human use. The corpus is never written to, and a missing
   `ocrmypdf`/Ghostscript is a note, not a failure.
3. **`eng+kan` is required, strictly.** No English-only fallback — a missing Kannada pack parks
   files as `needs_ocr` (capability gap, no attempts burned) until it is installed, so Kannada text
   is never silently garbled.

### Accepted consequence (not a question)

The `ocr_fingerprint` change re-processes the ~142 already-indexed files that have blank pages exactly
once: they are re-extracted, re-chunked and re-embedded on the first post-OCR run. That churn is
inherent to fixing pages that were indexed without text, and it is bounded — `pages_without_text`
returns to 0 for those files, after which they settle to `UNCHANGED`.
