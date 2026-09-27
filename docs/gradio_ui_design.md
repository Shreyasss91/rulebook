# Gradio UI — Design (v1.1.0)

Status: **plan** — not implemented. This is the design for the v1.1.0 milestone "Gradio UI with
citations": a local web front-end over the retrieval and answering logic that `scripts/query_cli.py`
already owns. The UI adds **no** retrieval, citation or LLM logic of its own — every question is
answered by the same `run_query` call the CLI uses, so the two front-ends can never disagree.

Related: `docs/query_cli_design.md` (the library this adapts), `docs/stack_choices.md` §5.7 (why
Gradio over Streamlit/Chainlit, and why the adapter must stay thin), `docs/ocr_pass_design.md`
(the searchable-PDF artefact a "view source" button can later serve).

---

## 1. Goal and principle

Give non-developer users the cited-answer workflow without a terminal:

```
browser ──► gradio Blocks ──► query_cli.run_query(...) ──► answer + [n] citations + sources
                (thin adapter: state, rendering, error mapping — nothing else)
```

**The adapter rule** (from `stack_choices.md` §5.7): everything answer-shaped lives in
`query_cli.py`; `scripts/query_ui.py` may only (a) map UI inputs onto `run_query` parameters,
(b) map a `QueryResult` onto UI components, and (c) map failures onto visible states. If a change
needs new retrieval behaviour, it lands in `query_cli.py` first and the UI inherits it — the hybrid
plan (§3.3 of `docs/hybrid_retrieval_plan.md`) is fused inside `Retriever.search` precisely so the
UI gets it for free.

## 2. Non-goals (v1.1.0)

- Conversation memory: each question runs independently; the chat history is display-only and is
  never sent to the LLM (matches the CLI's non-goal, and keeps answers reproducible).
- Multi-user / authentication / HTTPS: a single-user, local-only tool on the reference machine.
- Streaming tokens: `Llm.complete` is request/response; streaming would mean rewriting the LLM
  layer, not the UI. Revisit only when the LLM layer grows streaming.
- Serving the PDFs themselves (a "open the page" button) — v2 stretch; see §8.
- Any new retrieval, chunking or citation behaviour.

## 3. Dependency and version policy

- **Gradio 5.x** is the current major line (5.49.x as of late 2025); it supports Python 3.13 (the
  reference machine runs 3.13.5 — verified). Known install friction exists around gradio's pinned
  `pydantic` range on the newest Pythons; **verify the install on this machine at implementation
  time** and pin the working version in `requirements.txt` rather than floating.
- Gradio enters the **commented planned block** of `requirements.txt` now (this commit) and moves to
  the active block in the implementation commit — the same pattern sentence-transformers/chromadb
  followed, and it keeps `pip install -r requirements.txt` working today.
- The suite must not import gradio: the UI module imports it **lazily, inside `main()`/`launch()`**,
  so `tests/test_query_ui.py` can exercise the adapter's pure functions without the dependency (the
  established rule: the tests never need the heavy deps).
- `share=True` (gradio's public demo tunnel) is **forbidden by default** for this corpus — it would
  serve regulatory documents through gradio's CDN. Config controls it (§6) and the default is off.

## 4. Architecture

### 4.1 Module shape (`scripts/query_ui.py`)

```python
# importable WITHOUT gradio installed:
def sources_rows(result: QueryResult) -> list[list]:      # [n] | citation | score | cited?
def render_answer(result: QueryResult) -> str             # markdown + citation chips
def status_banner(result: QueryResult) -> str             # warnings / fallback reasons
def launch_args(config: dict) -> dict                     # server_name/port/share from config
def build_app(config, retriever, llm): ...                # needs gradio (called from main())
def main(argv=None) -> int: ...                           # lazy `import gradio`, then launch
```

- Startup (`main`): `ingest.load_config` → `query_cli.open_retriever` (validates deps, opens the
  collection, loads the embedder once) → `select_llm(config)` → `build_app(...)` → `demo.launch(**launch_args(config))`.
  This reuses the CLI's exact setup path, including its error messages, so a mis-configured machine
  gets the same diagnosis in the terminal as `query_cli.py` gives.
- Per question: `run_query(question, retriever, llm, top_k=..., min_score=..., contains=...,
  max_context_chars=config["llm_max_context_chars"])` — the *same* function the one-shot CLI and the
  REPL call.
- Concurrency: the answer handler is registered with `concurrency_limit=1` — the 8 GB reference
  machine must not load two embedder batches or two LLM contexts at once. Gradio's queue handles
  the rest.

### 4.2 Layout (Blocks)

```
┌ KERC Rule Book — cited Q&A ─────────────────────────────────────────────┐
│ status banner (setup errors / fallback reasons / warnings)              │
├──────────────────────────────────────────┬──────────────────────────────┤
│  Chatbot (history, display-only)         │ ⚙ Settings (accordion)       │
│  ┌ answer markdown, [n] as chips ┐        │  backend  [ollama|claude|none]│
│  └───────────────────────────────┘       │  top-k    [slider 1..20]     │
│  question textbox (+ submit)             │  min-score [slider 0..1]     │
│  examples row (a few real questions)     │  contains [textbox]          │
├──────────────────────────────────────────┴──────────────────────────────┤
│  Sources accordion: table [n] | citation | score | cited ✓              │
│    ▸ per-source expandable: full chunk text (the --show-context view)   │
└─────────────────────────────────────────────────────────────────────────┘
```

- **Chatbot + separate Sources table** rather than cramming sources into the chat bubble: the
  sources of the *last* question are the ones that justify its answer; older turns keep their
  inline `[n]` markers as plain text (chips only for the latest answer).
- Settings mirror the CLI flags (`--llm`, `-k`, `--min-score`, `--contains`); the backend dropdown
  offers exactly what `select_llm` accepts, and `none` shows the extractive answer.
- An examples row seeds the first run with questions known to hit the indexed corpus, so an empty
  UI is never the first impression.

## 5. Rendering

- **Answer**: markdown; each validated `[n]` marker becomes a small chip whose label is the target
  source's citation (`order.pdf, p.12`); markers `validate_citations` flagged as out-of-range are
  rendered in red and the warning surfaces in the status banner — the UI must not silently beautify
  a hallucinated citation.
- **Sources table**: one row per `Hit` — `[n]`, `hit.citation()`, `score` (3 decimals), `[cited]`
  when `hit.rank ∈ result.cited`. This is `QueryResult.to_dict()["sources"]` verbatim, so the UI and
  `--json` output can never diverge.
- **Expandable chunk text**: an accordion per source with the full chunk text — the `--show-context`
  view — so verification needs no terminal.
- **Page-less formats** need no special UI casing: `Hit.citation()` already renders
  `tariff.xlsx, sheet "Tariff 2024"` / headings; the UI just displays it.

## 6. Error and state mapping (mirror of the CLI's exit codes)

| CLI behaviour | UI behaviour |
|---------------|--------------|
| exit 1: deps missing / collection empty / cannot open | startup fails in the terminal with the CLI's message; if the app is somehow running, the question handler raises `gr.Error` with the same text — never a blank answer |
| exit 2: no relevant excerpts | the answer area shows "No relevant excerpts were found in the collection."; sources table empty |
| extractive fallback (+ reason on stderr) | the extractive answer renders normally **and** the status banner shows the reason (`ollama unavailable: …`) — a fallback must never look like a normal LLM answer |
| warnings (truncation, hallucinated `[n]`, no markers) | status banner lists them; banner is persistent until the next question |
| `mode: llm` | no banner (or an empty one) |

## 7. Config additions (`config.yaml`, when implemented)

```yaml
# --- Gradio UI (scripts/query_ui.py) --------------------------------------
ui_server_name: "127.0.0.1"   # loopback only — this corpus never serves a LAN
ui_server_port: 7860
ui_share: false               # gradio's public tunnel; keep off for regulatory documents
ui_max_question_chars: 2000   # reject absurd inputs client- and server-side
```

Retrieval/LLM knobs are **not** duplicated: `llm_top_k`, `llm_min_score`, `llm_max_context_chars`
and the `llm_*` backend keys are read exactly as the CLI reads them; the UI's settings widgets
override per request the way the CLI flags do. No hardcoded paths or backends, per the conventions.

## 8. Testing and rollout

- **Dependency-free tests** (`tests/test_query_ui.py`): `sources_rows` (paged, sheet and heading
  citations; `[cited]` flag), `render_answer` (chips for valid markers, red for out-of-range, plain
  text survives), `status_banner` (all §6 rows), `launch_args` (loopback defaults, `ui_share` false
  by default, port/name from config), and that `build_app`/`main` import gradio only lazily (assert
  the module imports with gradio absent). Gradio's own event wiring is verified by a documented
  manual smoke test, not by the suite.
- **Rollout**: implementation commit adds `scripts/query_ui.py`, moves `gradio` to the active block
  (pinned), adds the `ui_*` config block and the tests; CHANGELOG's Planned entry flips to Added;
  CLAUDE.md's milestone table marks v1.1.0 done. The CLI remains the interface of record.
- **Stretch (v1.x, separate)**: a "view source" button per citation that serves the searchable PDF
  from `ocr_pdf_path` (`file.pdf#page=N`) — only for files the OCR pass mirrored; page-less formats
  simply have no button.

## 9. Risks

| Risk | Mitigation |
|------|------------|
| Gradio's pinned deps clash on Python 3.13 | Verify install before implementation; pin the working version; the adapter's core is testable without gradio so a gradio problem never blocks the suite |
| UI drifts from CLI behaviour | The adapter rule (§1): only `run_query` answers questions; sources come from `to_dict()` |
| `share=True` leaks documents | Default `false` in config and code; called out in the config comment |
| Concurrent questions exhaust 8 GB | `concurrency_limit=1` on the answer handler |
| Non-developer users hit a setup error | Startup reuses the CLI's diagnostics verbatim (which exact dependency/ingest step to run) |

## 10. References

- Gradio Blocks / ChatInterface docs (gradio.app/docs) — Blocks chosen over ChatInterface for the
  settings sidebar and sources table (§4.2).
- `docs/stack_choices.md` §5.7 — the UI comparison and the thin-adapter recommendation this
  implements.
- `docs/query_cli_design.md` — the library being adapted; its §6/§8 notes are the source of the
  error-mapping table.
