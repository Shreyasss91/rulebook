# OCR Pass — Design

Status: **implemented** (2026-09-26) in `scripts/incremental_ingest.py` — §13 records what
shipped and where it deviates from the plan below. It has *not* been run against the real corpus
yet; that is a deliberate manual step (§11 step 4).

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
ocr_api_url: "https://api.novita.ai/v3/openai/chat/completions"
ocr_api_model: "deepseek/deepseek-ocr-2"
ocr_api_key_env: "NOVITA_API_KEY"  # read from the environment, never from config.yaml
ocr_api_timeout: 60
ocr_api_cost_per_page: 0.0001      # only used for the run's cost estimate
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

1. **Spike (not done yet).** Run Tesseract on ~20 pages from `OMBUDSMAN ORDERS`/`KPTCL ESCOMS`
   and compare against Novita on the same pages, to confirm quality assumptions in
   `docs/origin_doc.md` on the real scans. Cheap, and it can change the default backend.
2. **Phase 1 — page-level pass + cache + fingerprint + tests**, with the backend reading `hybrid`
   from config from the start; escalation simply cannot engage until `NOVITA_API_KEY` is exported, so
   a keyless machine still runs Tesseract-only and nothing paid happens by accident.
3. **Phase 2 — enable the Novita escalation path** (the paid branch of `hybrid`) once the key exists.
4. **Phase 3 — run it.** `--source` one folder with `--ocr-limit`, then the full drive. Requires the
   external `D:` drive mounted and Tesseract installed; this is a deliberate manual run, not
   something the script does on its own. Update `docs/kerc_folder_inventory.md` with the real
   recovered-page counts.
5. Only after that is `needs_ocr` expected to drain to near zero, and the milestone closed.

External prerequisites for step 4: the `D:` drive mounted, the Tesseract binary with the **Kannada**
traineddata (`kan`) installed, Ghostscript (for `ocrmypdf` artefacts), and `NOVITA_API_KEY` exported
for the `hybrid` escalation.

## 12. Decision log

Three choices were put to the user on 2026-09-26, each with its consequences spelled out, before any
code was written. The chosen option is marked ✅. Keep this section when the implementation lands —
the reasoning is the part that is expensive to reconstruct.

### Q1 — Default OCR backend for the first real run

| Option | Consequences considered | Decision |
|--------|-------------------------|----------|
| **A. `hybrid` — Tesseract + Novita escalation** | Free Tesseract carries the bulk; only low-confidence/table/Kannada pages reach the paid API, so total cost stays ~$0.19–0.64. Requires `NOVITA_API_KEY`; without it the run degrades to Tesseract-only with a note. Escalated pages (only) leave the machine, so privacy is partial — and only for the pages Tesseract already failed. Slower than API-only on the escalated subset. | ✅ **chosen** |
| **B. Tesseract only** | Zero cost, fully local, no key to manage, simplest to reason about. Weaker on tables, faded scans and mixed Kannada/English — the exact material in `OMBUDSMAN ORDERS` and `KPTCL ESCOMS`. Would leave visible quality gaps, and adding Novita later re-processes those pages (an `ocr_fingerprint` change), i.e. pays the work twice. | rejected |
| **C. Spike decides** | Run the ~20-page Tesseract-vs-Novita comparison first and pick from real quality, rather than from `docs/origin_doc.md`. Most evidence-driven, but delays a decision the implementation needs, and the cost to change later is one fingerprint-triggered re-run, which makes waiting low-value. The spike is still kept as step 1 of the rollout — it can *confirm* the choice cheaply. | rejected as a blocker; retained as validation |

Notably, A makes the fallback path explicit: a missing key degrades rather than stalls, so the
"no accidental spend" property does not depend on the default alone.

### Q2 — Where OCR text lives long-term

| Option | Consequences considered | Decision |
|--------|-------------------------|----------|
| **A. Page cache only** (`data/ocr_cache/`) | Cheapest and smallest: no duplicated PDFs, nothing but text on disk, cache keyed by `content_hash` so re-runs are free. Enough for retrieval and page-exact citations. Leaves the corpus unsearchable for a human opening a PDF in Acrobat/Edge — the scans stay image-only forever, and every future consumer (a different RAG stack, a manual lookup) re-runs OCR. | superseded by B |
| **B. Cache + `ocrmypdf --skip-text` searchable PDFs** | Adds a human-usable artefact: the affected PDFs become text-searchable in any reader, permanently, free and local (`ocrmypdf`/Tesseract — no API cost). Costs disk and a new dependency chain (`ocrmypdf` + Ghostscript). Mitigated by mirroring **only** the ~285 files that actually had pages OCR'd (not all 994), writing to `data/ocr_pdfs/` and never into the corpus, and by treating the artefact as best-effort (missing Ghostscript is a note, not a failure). `--skip-text` keeps the PDF pass page-aligned with the text pass. | ✅ **chosen** |

The chosen option also fixes the long-term home of the text: the cache is the retrieval source of
truth, so nothing downstream depends on the artefact being present.

### Q3 — How to handle Kannada

| Option | Consequences considered | Decision |
|--------|-------------------------|----------|
| **A. `eng+kan`, fall back to `eng` with a note** | Never blocks: a missing pack still yields English text for the English portions, and the note records the gap. But the Kannada half of a mixed order is silently garbled — worse than absent, because it produces confident-looking wrong text that citations would then point at. | recommended, rejected |
| **B. `eng+kan` strictly** | No English-only fallback: an engine whose `kan` data is missing reports *not available*, so affected files park as `needs_ocr` (a capability gap — no `attempts` burned, per §7) until the pack is installed. Guarantees text that is either correct or flagged, never quietly mangled. Cost: nothing is OCR'd until the Kannada traineddata (and the key, for escalation) is in place. | ✅ **chosen** |
| **C. `eng` only for now** | Simplest first pass, no additional traineddata to install. Leaves Kannada portions of mixed orders unsearchable — a permanent blind spot in a corpus that is explicitly bilingual, and one that is hard to notice later because the files would look successfully indexed. | rejected |

### Not a question — accepted consequence

The `ocr_fingerprint` change re-processes the ~142 already-indexed files that have blank pages exactly
once: they are re-extracted, re-chunked and re-embedded on the first post-OCR run. That churn is
inherent to fixing pages that were indexed without text (the alternative — a full re-ingest — is
strictly worse), and it is bounded: `pages_without_text` returns to 0 for those files, after which
they settle to `UNCHANGED`.

---

## 13. Implementation notes (2026-09-26)

Landed in `scripts/incremental_ingest.py` with a test section in `tests/test_incremental_ingest.py`
(engine, renderer, cache and `ocrmypdf` all faked — the suite still needs no binary, key or network).

- **Where it runs** — `OcrRunner.fill()` is called from `process_new_or_modified`, between
  `extract_pages()` and `build_chunks()`, mutating the same page dicts. Chunk ids, page numbers and
  citations were therefore untouched.
- **Engines** — `TesseractEngine` (pytesseract; `available()` requires *every* configured language via
  `get_languages()`, so a missing `kan` pack parks files instead of degrading) and `NovitaEngine`
  (stdlib `urllib.request`, OpenAI-compatible chat payload, key read from `ocr_api_key_env`).
  `build_ocr_engines()` maps the backend to a priority list; `main()` drops unavailable engines, so
  `hybrid` degrades to whichever engine is actually present.
- **Rendering** — `render_pdf_page()` uses pdfplumber's `to_image(resolution=ocr_dpi)`, so no poppler.
- **Cache** — `OcrCache` stores one JSON per source file under `ocr_cache_path`, carrying
  `content_hash` + `fingerprint`; any mismatch discards the whole store rather than reusing part of it.
  Empty text is cached on purpose *when an engine ran successfully and read nothing*, so a hopeless
  page is never charged for twice; a page whose engine **failed** is deliberately not cached, so a
  transient failure is retried instead of being mistaken for an empty page.
- **Fingerprint** — `ocr_fingerprint()` hashes backend, dpi, languages, per-page threshold,
  escalation threshold, the API endpoint/model **and the names of the usable engines**, so adding a
  Novita key (or pointing `ocr_api_url`/`ocr_api_model` at a different service) re-visits pages that
  were only read by the previous setup. This bumped `SCHEMA_VERSION` to 2 (older manifests load fine;
  it is 3 as of the embedding guard-rail in `docs/stack_choices.md`).
- **Settling rule** — the fingerprint is written only when candidates existed, none were left
  behind, and no engine failed (`attempted and not incomplete and not errors and not fatal`). A run
  cut short by `--ocr-limit`/`ocr_max_pages_per_file`, or one where an engine errored on a page,
  stays un-settled so the next run finishes it — and because the pages already done are cache hits,
  the overlap is free rather than double-charged.
- **Statuses** — `needs_ocr` when OCR recovered nothing because nothing was readable (a
  capability/bad-scan gap: no attempts burned, retried only once the fingerprint changes); `error`
  when an engine or the PDF open failed, which is retried (ceiling `max_retries`) rather than settled.
  Partial recovery indexes what came back and records the remainder in `pages_without_text`.
- **Searchable PDFs** — `ensure_searchable_pdf()` calls `ocrmypdf --skip-text`, mirrors only files
  that have OCR text (recovered this run *or* answered from the cache, so a re-run that only hits the
  cache still emits the artefact), and records `ocr_pdf_hash` + `ocr_pdf_fingerprint` so it is not
  regenerated for unchanged content. A missing `ocrmypdf` is reported once per run, not noted against
  every file.
- **Cost control** — `--no-ocr`, `--ocr-limit N`, and the guarantee that `--dry-run`/`--audit` never
  build an engine, render a page or touch the cache. `--ocr-limit` counts *attempted* pages, so a
  read that comes back empty still draws down the budget (an empty result can still have cost a paid
  call). `report_ocr()` prints pages OCR'd, cache hits, engine calls, escalations, failed pages,
  deferred pages and an estimated API cost.

### Deviations from the plan above

- `OCR_AVAILABLE` is kept as a module-level fallback (`False`) for direct callers and tests; real runs
  pass an explicit `{"available", "fingerprint"}` context into `classify()`. Nothing enables OCR
  implicitly.
- `extract_pages()` keeps its two-argument signature — OCR is a separate post-step, so existing
  callers and monkeypatched tests are unchanged.
- An unknown `ocr_backend` value warns and falls back to `hybrid` rather than guessing silently,
  because the wrong guess can spend money.
- The endpoint and model are config values defaulting to Novita's OpenAI-compatible chat endpoint and
  `deepseek/deepseek-ocr-2`; the §11 spike should confirm the model string before the real run.
