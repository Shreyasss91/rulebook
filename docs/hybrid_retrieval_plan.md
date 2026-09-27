# Hybrid Retrieval — Plan (BM25 + dense, fused with RRF)

Status: **plan** — not implemented. This executes the "Hybrid keyword (BM25) retrieval" item in
CHANGELOG's Planned block and the decision in `docs/stack_choices.md` §5.3: dense + `$contains` is
right for v1, but lexical matching beyond exact substrings (Kannada terms, act names, tariff
phrases that embed poorly) needs a real keyword index. The plan deliberately changes **no** code.

Related: `docs/query_cli_design.md` §8 (the deferred item this plan implements), §3 (the
`Retriever.search` seam this attaches to), `docs/kannada_retrieval_plan.md` (the embedder switch
this is coordinated with), `docs/stack_choices.md` §2 (why not LanceDB for this).

---

## 1. The problem

Dense retrieval matches *meaning*; it is weakest exactly where legal questions are strongest:

| Query | Dense behaviour | BM25 behaviour |
|-------|-----------------|----------------|
| "Rule 14(3) late payment surcharge" | Rule numbers tokenize into noise; the semantic match may drift to any surcharge discussion | Exact term match on `14(3)`-adjacent tokens; precise |
| A Kannada statutory phrase | Depends entirely on the embedder (broken today per `stack_choices.md` §3) | Token overlap works for any language with word boundaries |
| Rare act/order names | Names dominate the embedding weakly | Names are high-IDF terms — BM25's strongest case |

`--contains` already covers the single most important case (a verbatim rule number) but it is a
*filter*, not a ranker: it returns nothing when the phrase is slightly different, and contributes no
scoring when it matches hundreds of chunks.

## 2. The decision: hand-rolled BM25 on SQLite FTS5, fused with RRF

| Option | Verdict |
|--------|---------|
| **SQLite FTS5 + BM25, fused with RRF** *(chosen)* | The standard-library `sqlite3` on this project's Python 3.13 ships FTS5 (verified on the reference machine: `CREATE VIRTUAL TABLE … USING fts5` succeeds); BM25 is FTS5's native ranking function; zero new dependencies; one extra file on disk |
| LanceDB native hybrid (`stack_choices.md` §2.1) | The better *product*, but a store migration — a dependency chain and a rewrite of `open_collection`/upsert/delete — to gain one feature. Revisit only if the hand-rolled index hurts (the §2 trigger stands) |
| BGE-M3 sparse vectors | Ties retrieval quality to a 568M-model embedder switch (`kannada_retrieval_plan.md` §2 treats it as the escalation, not the first step) |
| Chroma's `$contains` (today) | Keep — it stays as the exact-filter escape hatch and the fallback when the FTS index is missing |
| A hosted/managed search service | Rejected by local-first |

## 3. Design

### 3.1 Storage: one FTS5 database, external-content

A single SQLite file (`data/fts_index/kerc_fts.sqlite3`, gitignored with `data/`) holding an
**external-content** FTS5 table over a plain row table, so rows can be deleted by `source` with
ordinary SQL (FTS5's own `delete` API needs the original lexed values):

```sql
CREATE TABLE chunks (            -- plain table: source of truth for deletes/updates
  chunk_id TEXT PRIMARY KEY,     -- = make_chunk_id(rel, content_hash, i), same as Chroma's ids
  source TEXT NOT NULL,          -- rel path, same value as Chroma metadata
  page INTEGER NOT NULL,         -- -1 for page-less formats
  section TEXT NOT NULL,
  text TEXT NOT NULL
);
CREATE INDEX idx_chunks_source ON chunks(source);
CREATE VIRTUAL TABLE chunks_fts USING fts5(text, content='chunks', content_rowid='rowid');
```

- **Chunk ids are already stable** (`sha1(rel|hash|index)`), so an FTS row and its Chroma twin share
  an id — fusion joins on rank lists, and idempotent upserts are `INSERT OR REPLACE`.
- Triggers (`AFTER INSERT/UPDATE/DELETE ON chunks`) keep `chunks_fts` in sync — the standard
  external-content pattern.
- Size: ~37k chunks ≈ the corpus text once more on disk (~100–200 MB at full corpus scale, measured
  at backfill time and recorded in §8); trivially affordable.
- Tokenizer: the default `unicode61`. Kannada uses spaces between words, so word tokenization
  applies; no Kannada stemmer exists in FTS5 and none is needed for BM25's term-overlap job.
  If tokenizing ever proves wrong for Kannada compounds, `trigram` is the documented fallback
  (decide from eval evidence, not speculation).

### 3.2 Write path: mirror the Chroma operations in `incremental_ingest.py`

The FTS store is a **shadow of the collection**, updated at exactly the same four points:

| Pipeline event | Chroma call | FTS call |
|----------------|-------------|----------|
| NEW / MODIFIED (after OCR + chunking) | `delete(where source)` + `upsert(ids…)` | `DELETE FROM chunks WHERE source = ?` + insert the same `documents`/metadata already in hand |
| DELETED | `delete(where source)` | `DELETE FROM chunks WHERE source = ?` |
| MOVED | metadata re-label, no re-embed | `UPDATE chunks SET source = ? WHERE source = ?` — no re-tokenization |
| re-embed (fingerprint change) | full re-upsert per file | nothing extra: text unchanged, the per-file replace above covers it |

Rules:

- Write FTS **only when the chunk upsert actually happened** (i.e. inside the same code path as the
  Chroma write), so the two stores cannot diverge in *content* — an FTS write for a
  `pending_embedding` file would desync the mirror.
- Without the embedding stack, files stay `pending_embedding` and get no FTS rows either — the FTS
  store mirrors *indexed* chunks, not extracted text.
- A crash mid-run is safe: every FTS write is keyed by content-derived ids and re-runs are
  idempotent replaces, the same property the Chroma path already relies on.
- `--dry-run`/`--audit` touch nothing, as everywhere else.

### 3.3 Read path: one optional fusion step inside `Retriever`

`Retriever.search` gains a second, optional candidate list:

1. Dense list: unchanged (embed → `collection.query` → cosine score → filter).
2. Keyword list (only if hybrid is enabled **and** the FTS file opens **and** FTS5 is available):
   `SELECT chunk_id FROM chunks_fts WHERE chunks_fts MATCH ? ORDER BY bm25(chunks_fts) LIMIT ?`
   with the query mapped to a quoted-phrase `MATCH` expression; joined to `chunks` for the metadata.
3. **Fusion = Reciprocal Rank Fusion**: `score = Σ 1/(rrf_k + rank_i)` over both lists
   (`rrf_k = 60`, config `hybrid_rrf_k`). RRF is chosen because BM25 and cosine scores are not
   comparable on one axis; only ranks are fused, which needs no score calibration and no new maths
   to defend. The fused list is truncated to `top_k`; `Hit.score` carries the RRF score for display
   (the CLI already prints scores without interpreting them).
4. `--contains`/`where_document` keeps its current semantics (a hard filter over the *fused*
   result), so the exact-lookup escape hatch survives unchanged.

Failure modes are explicit and all degrade to today's behaviour: missing FTS file → dense-only +
one warning; FTS5 missing (a non-standard Python build) → feature self-disables; FTS file present
but row-count wildly off the collection count (someone deleted `kerch_db` or the FTS file) →
warning + a documented rebuild command, never a wrong answer.

### 3.4 Config (nothing hardcoded)

```yaml
# --- Hybrid retrieval (plan: docs/hybrid_retrieval_plan.md) ---------------
hybrid_retrieval: false          # off until the eval (§4) passes; flip to enable
hybrid_path: "data/fts_index"    # gitignored, like the OCR cache
hybrid_rrf_k: 60                 # RRF smoothing constant
```

`Retriever` reads these the way it reads `llm_top_k` — absent keys mean defaults, so old configs
keep working.

### 3.5 Backfill

The FTS store is built **from the collection, not the corpus**: `collection.get()` page by page
(documents + metadatas carry everything §3.1 needs). No re-extraction, no OCR spend, no manifest
change — the backfill is a maintenance subcommand (`incremental_ingest.py --rebuild-fts`) runnable
any time, and it is also the rebuild path for §3.3's desync warning.

## 4. The eval (shares the Kannada plan's yardstick)

`docs/kannada_retrieval_plan.md` §4 builds a 20-query recall@6 set; its **rule-number / statutory-
phrase queries** are exactly hybrid's target cases. Procedure:

1. On the dense-only baseline, record recall@6 for the lexical subset.
2. Enable hybrid (`hybrid_retrieval: true`), re-run the full set.
3. Gates: lexical subset **+15 points or more** recall@6, no subset worse than **−2 points**
   (semantic queries that BM25 pollutes are the regression risk RRF's rank fusion usually absorbs —
   verify, don't assume), and per-query latency still dominated by the LLM, not retrieval.
4. Record the numbers in §8 of both plans (they share one eval set by design).

Only then flip `hybrid_retrieval: true` in `config.yaml` as the default.

## 5. Coordination with the embedder switch

The two milestones are independent in mechanism but deliberately share a window
(`kannada_retrieval_plan.md` §5, `stack_choices.md` §5.3):

- The FTS backfill reads the *collection*, so it can run before, with, or after the re-embed — the
  only ordering constraint is backfill-after-some-index-exists.
- If both land together: run the embedder switch first (its own eval gates retrieval quality),
  backfill FTS from the new `kerc_docs_multi` collection, then evaluate hybrid. One corpus
  migration, two improvements, and the hybrid eval measures the *new* embedder's dense baseline —
  the honest comparison for what hybrid adds.
- If hybrid lands alone: same procedure against the existing collection.

## 6. Risks and mitigations

| Risk | Mitigation |
|------|------------|
| FTS5 absent in a deployment's Python | Feature self-disables with one warning; dense + `$contains` remains; the probe is a 2-line try/except, the same pattern as the OCR engines |
| Two stores drift apart | FTS written only inside the Chroma write path (§3.2), content-keyed ids make every write idempotent, row-count check warns on desync, `--rebuild-fts` repairs |
| BM25 pollutes good dense results | RRF fuses ranks, not scores; the eval gate is per-subset (§4); `hybrid_retrieval` stays a config flip off |
| `MATCH` syntax injection from user queries | The query is mapped to a quoted phrase and, on `fts5` syntax errors, that term list falls back to dense-only with a warning — never a crash |
| Windows path/locking quirks with a second SQLite writer | Single-writer by construction: only the ingest process writes; the query side opens read-only |
| Duplicated retrieval logic between CLI and the future UI | Fusion lives inside `Retriever.search`, the same seam `docs/gradio_ui_design.md` hangs the UI on — the UI inherits hybrid for free |

## 7. Out of scope

- Cross-encoder re-ranking (a second model per query — the step *after* hybrid, if the eval shows
  fused ranking is still the bottleneck).
- LanceDB/Qdrant migration for native hybrid — the §2 trigger only fires if the hand-rolled index
  proves painful.
- Query rewriting / HyDE; multi-hop retrieval.
- Any change to the embedder, chunker or OCR pass.

## 8. Results (fill in at execution)

| Metric | Dense-only | Hybrid (RRF) | Gate |
|--------|------------|--------------|------|
| Lexical subset recall@6 | — | — | ≥ +15 pts |
| Semantic subset recall@6 | — | — | ≥ −2 pts |
| Full eval set MRR | — | — | — |
| p50 retrieval latency | — | — | LLM-dominated |
| FTS file size | — | — | — |

## 9. References

- FTS5 external-content tables, triggers, `bm25()` ranking: sqlite.org/fts5.html.
- Reciprocal Rank Fusion: Cormack et al., *"Reciprocal Rank Fusion outperforms Condorcet and
  individual Rank Learning Methods"* (SIGIR 2009) — the standard `k=60` constant comes from that
  paper.
- The deferred-item history: `docs/query_cli_design.md` §2/§8; the analysis behind "keep ChromaDB,
  hand-roll keyword": `docs/stack_choices.md` §2.2–§2.3, §5.3.
- The eval methodology this shares: `docs/kannada_retrieval_plan.md` §4.
