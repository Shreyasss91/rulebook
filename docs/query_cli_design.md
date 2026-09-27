# Query CLI — Design

Status: **implemented** in `scripts/query_cli.py` (v1.0.0 of the milestones in `CLAUDE.md`). This
document is the plan the code follows; §8 records deviations and open items.

Related reading: `docs/origin_doc.md` (architecture, LLM choices), `docs/incremental_update_strategy.md`
(the manifest/collection the query reads), `docs/ocr_pass_design.md` (why some pages were scanned).

---

## 1. Goal

Turn the indexed corpus into cited answers:

```
question ──► embed ──► vector search (ChromaDB) ──► numbered excerpts ──► LLM ──► answer + [n] citations
                                                                      └──► extractive fallback
```

The retrieval side already exists: `scripts/incremental_ingest.py` writes chunks with
`source` (relative path), `page` (exact page number, `-1` for page-less formats), `section` (the
heading, or the sheet name for spreadsheets), `file_type` and `content_hash`. The CLI adds no new
storage and no new index — it reads the same `kerch_db/` collection the ingest wrote.

**Requirement**: every claim must be traceable to a file **and page**. That is the whole point of the
legal-aware chunking the pipeline already does, so the query side must not lose it.

## 2. Non-goals (v1.0.0)

- A different vector store or embedding model — reuse `embedding_model`/`collection_name` from config.
- Re-ranking with a cross-encoder, BM25/hybrid keyword search, query rewriting, multi-hop. Noted for
  a later milestone (§8); the seam (`Retriever.search`) is where they would attach.
- Streaming tokens to the terminal, conversation memory, tool use.
- The Gradio UI (v1.1.0) — the CLI is the library plus a thin front-end.
- Any new hard dependency: the HTTP calls use the standard library (`urllib.request`), exactly like
  `NovitaEngine` in the OCR pass.

## 3. Retrieval

`Retriever.search(question, top_k, min_score, contains)`:

1. Embed the question with the same `all-MiniLM-L6-v2` model used at index time (a different model
   would silently make scores meaningless, so it is read from the same config key).
2. Query the collection for a **candidate pool** of `top_k × 4` (capped at 100), include
   `documents`, `metadatas`, `distances`.
3. Convert Chroma's **cosine distance** to similarity `score = 1 − distance` (the collection is
   created with `hnsw:space: cosine` in `open_collection`). Distances are compared, never a raw
   score, so `min_score` is a similarity threshold.
4. Drop hits below `min_score`, keep the best `top_k`.
5. If `--contains` is given, pass Chroma's `where_document={"$contains": phrase}` so an exact rule
   number or a quoted phrase can be found even when the embedding misses it. This is the cheap 80%
   of "hybrid search"; a real BM25 index is deferred (§8).

Each hit is a `Hit` dataclass carrying the chunk text plus the metadata needed to cite it.

## 4. Page-exact citations

A `Hit.citation()` renders the one thing a reader needs to verify a claim:

| Metadata | Citation |
|----------|----------|
| `page > 0` | `file.pdf, p.12` |
| `page == -1` and `section` (a worksheet name) | `file.xlsx, sheet "Tariff 2024"` |
| `page == -1`, no section (`txt`/`md`/`docx`) | `file.txt` (whole-document) |
| `page == -1`, section is a heading | `file.docx, <heading>` |

The excerpts are numbered `[1]…[k]` in the prompt; the model is instructed to cite with those
markers. After generation the answer's markers are **validated**: a marker outside `1..k` is a
hallucinated citation and is reported as a warning, and an answer with no markers at all is flagged
too. The CLI always prints the retrieved sources with their scores, so a reader can check an answer
even when the model misbehaves.

## 5. Answer generation

| Backend | Config value | Cost | Why |
|---------|--------------|------|-----|
| **Ollama** (default) | `llm_backend: "ollama"` | $0 | Local-first: regulatory documents never leave the machine, and no key to manage |
| Claude API | `llm_backend: "claude"` | per token | Better reasoning for hard questions; key read from the environment, never config |
| None | `llm_backend: "none"` (or `--no-llm`) | $0 | Retrieval-only / extractive answer, and the mode the tests run in |

The default is Ollama because the project is explicitly local-first (`docs/origin_doc.md`); Claude is
one config line away when quality matters more than locality.

**Degradation is explicit, never silent.** If the chosen engine is not usable — Ollama not running,
no `ANTHROPIC_API_KEY`, HTTP/parse error — the CLI still returns the retrieved excerpts as an
extractive answer and prints the reason on stderr. A question with no hits returns exit code 2.

### Prompt

The system prompt pins the model to the excerpts ("answer using ONLY the numbered excerpts"), asks
for inline `[n]` markers, and requires it to say when the excerpts do not answer. The user turn is
`Excerpts: … Question: …`, with the total context capped by `llm_max_context_chars` so a 500-page
tariff order cannot blow the model's context window.

## 6. CLI

```bash
python scripts/query_cli.py "what is the late payment surcharge?"   # one question
python scripts/query_cli.py -i                                      # interactive
python scripts/query_cli.py "..." --show-context                    # print the full excerpts
python scripts/query_cli.py "..." --json                            # machine-readable
python scripts/query_cli.py "..." --no-llm                          # retrieval only
python scripts/query_cli.py "Rule 14(3)" --contains "Rule 14(3)"    # exact substring
python scripts/query_cli.py "..." -k 10 --min-score 0.35 --llm claude
```

- `-k/--top-k`, `--min-score`, `--contains`, `--show-context`, `--json` shape retrieval and output.
- `--no-llm` / `--llm ollama|claude|none` override the configured backend for one run.
- Exit codes: `0` answered, `1` setup error (deps, index missing/empty), `2` no relevant excerpts.

## 7. Config additions (`config.yaml`)

```yaml
# --- Query CLI (scripts/query_cli.py) ------------------------------------
llm_backend: "ollama"          # ollama | claude | none
llm_top_k: 6                   # excerpts retrieved per question
llm_min_score: 0.0             # cosine similarity floor; 0 drops anti-correlated chunks
llm_max_context_chars: 6000    # cap on excerpt text sent to the model
llm_ollama_url: "http://localhost:11434"
llm_ollama_model: "llama3.2:3b"
llm_ollama_timeout: 120
llm_claude_url: "https://api.anthropic.com/v1/messages"
llm_claude_model: "claude-sonnet-4-5"   # override with a current id from platform.claude.com
llm_claude_api_key_env: "ANTHROPIC_API_KEY"
llm_claude_max_tokens: 1024
llm_claude_timeout: 120
llm_anthropic_version: "2023-06-01"
```

No new hard dependencies: `sentence-transformers`/`chromadb` are the existing optional indexing
stack, and the HTTP providers use the standard library.

## 8. Testing, deviations and open items

- **Testing** stays dependency-free: a fake collection (`query`/`count`), a stub embedder and a fake
  `urllib.request.urlopen` exercise retrieval, scoring, the `where_document` filter, citation
  rendering (including `page = -1`), context capping, both LLM payloads, citation validation, the
  extractive fallback and an end-to-end `main()` run.
- **Deviation from `docs/origin_doc.md`**: hybrid BM25 search and cross-encoder re-ranking are not
  built; the `--contains` exact-substring filter covers the most important case (looking up a rule
  number verbatim) without a second index. They are the next retrieval milestone.
- **Open item**: `llm_claude_model` ships with a dated-looking default (`claude-sonnet-4-5`).
  Anthropic model ids change; set the current one from the platform docs before using
  `llm_backend: "claude"`. The Ollama default (`llama3.2:3b`) is chosen to fit the 8 GB reference
  machine; a larger model helps on a roomier box.
