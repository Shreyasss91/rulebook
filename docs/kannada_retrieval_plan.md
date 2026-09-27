# Kannada Retrieval — Migration Plan

Status: **plan** — not implemented. This is the executable plan for the gap identified in
`docs/stack_choices.md` §3: the corpus is bilingual `eng+kan`, the embedder (`all-MiniLM-L6-v2`) is
English-only, so every Kannada passage retrieves poorly no matter how good the chunking, OCR and
citations are. The plan deliberately changes **no** code and **no** config yet: it is a milestone
(a re-index), so it should be executed deliberately, not defaulted into.

Related: `docs/stack_choices.md` §3 (the analysis this plan executes), `docs/ocr_pass_design.md`
(why the text layer is already `eng+kan`-strict), `docs/query_cli_design.md` (the query side this
must not break), `docs/hybrid_retrieval_plan.md` (scheduled to ride the same re-index).

---

## 1. The problem, precisely

KERC orders routinely mix English and Kannada. The OCR pass enforces a strict `eng+kan` language
pack (never an English-only fallback), so the *text* side is correct. The *retrieval* side throws
that work away, in three distinct ways:

| Query | Target chunk | Failure with `all-MiniLM-L6-v2` |
|-------|--------------|----------------------------------|
| Kannada question | Kannada passage | The model never saw Kannada; its tokenizer emits byte-fallback tokens and similarity is near-random |
| English question about a Kannada order | Mixed chunk | Only the ASCII fragments (rule numbers, authority names) can match; the semantic content is invisible |
| English question | Purely Kannada chunk | Recall is effectively zero — cross-lingual retrieval does not exist in this model |

The failure is invisible in English-only spot checks against English chunks, which is exactly how
the CLI has been exercised so far. It surfaces only when a user asks in Kannada or asks about the
Kannada portions of an order — the parts the OCR pass worked hardest to recover.

## 2. The decision: `multilingual-e5-small` first

| Model | Params / dims | Context | Why / why not as the first switch |
|-------|---------------|---------|------------------------------------|
| **`multilingual-e5-small`** *(chosen)* | 118M / 384 | 512 | 100 languages from XLM-R (Kannada included), 384 dims like today, Apache-2.0, CPU-friendly on the 8 GB reference machine; e5-family retrieval objectives score well on MTEB |
| `multilingual-e5-base` | 278M / 768 | 512 | The escalation if `small` under-delivers on the eval (§5); still CPU-fine, but 2× the index size |
| `BGE-M3`, `nomic-embed-text-v2-moe` | 568M+ | 8K | The *target state* per `stack_choices.md` §3 — heavier, and BGE-M3's sparse vectors belong to the hybrid milestone; go here only if e5 fails the eval |
| `EmbeddingGemma-300M` | 300M / 768 | 2K | Strong efficiency pick; Matryoshka truncation is not needed at 37k chunks |
| APIs | — | — | Rejected by local-first (`stack_choices.md` §1) |

**Why e5-small and not the biggest model directly:** the migration machinery below (prefixes,
fingerprint, eval, re-index) is model-agnostic and is the actual work. Proving it with the smallest
multilingual model that plausibly fixes Kannada keeps the first re-index cheap and reversible; the
eval (§4) decides whether to stop there or escalate. Dimensions and language coverage must be
re-verified against the current model card at execution time — treat the table above as a shortlist,
not a spec sheet.

## 3. What actually changes in code (the real work)

A config edit alone is **not** enough. Three things must change, and one of them is a real gap:

### 3.1 The prefix trap (mandatory, easy to get silently wrong)

The e5 model card is explicit: input texts **must** be prefixed — `query: ` for questions,
`passage: ` for indexed documents — or retrieval performance degrades. Today both the ingest path
(`index_chunks` → `Embedder.encode`) and the query path (`Retriever.search` → the same
`Embedder.encode`) call one `encode()` with no notion of prefixes. The obvious refactor — apply
`query: ` everywhere — would embed the *corpus* with the query prefix, which is the wrong one.

The fix, when implemented:

- `Embedder.encode(texts, *, is_query: bool = False)` (or sentence-transformers' `prompt_name`,
  supported in the installed 3.0.1 line — verify at execution): ingest passes `is_query=False`,
  the query CLI passes `is_query=True`. Each call site states its intent exactly once.
- The prefixes live in `config.yaml`, not code (no hardcoded anything):
  `embedding_query_prefix: ""` / `embedding_passage_prefix: ""` — empty today, so behaviour with
  MiniLM is byte-identical; set to `"query: "` / `"passage: "` when switching.

### 3.2 The fingerprint must cover the passage prefix

`embedding_fingerprint()` hashes only the model name. If prefixes were config-only, editing the
prefix would not re-embed — the exact class of bug the fingerprint exists to prevent. When the
prefix support lands, the fingerprint becomes `sha1(model + "|" + passage_prefix)[:12]`:

- **passage prefix included** — it shapes every stored vector; changing it must re-embed.
- **query prefix deliberately excluded** — it shapes no stored vector; a query-prefix-only edit
  degrades scores without corrupting the store. That failure mode is caught by the eval set (§4),
  and re-embedding the whole corpus to change a question-side string would be the wasteful choice.

`SCHEMA_VERSION` bumps once more when this lands; older manifests still load, per the established
pattern (`ocr_fingerprint` v2, `embedding_fingerprint` v3).

### 3.3 Same dimensions make the guard-rail load-bearing

e5-small is 384-dim like MiniLM, so the existing collection could physically hold the new vectors.
That is precisely what makes this switch dangerous: a reused collection would silently mix two
vector spaces with no visible symptom. The `embedding_fingerprint` guard-rail (already implemented,
manifest v3) is what makes the migration safe — it re-processes every indexed file because the
fingerprint no longer matches, even though no content hash changed. Execute the migration with a
fresh `collection_name` (`kerc_docs_multi`) anyway, so the old collection stays intact for rollback
and comparison until the eval passes.

### 3.4 The token-window check (the §5.2 open item)

`chunk_size: 1200` chars ≈ 300 English tokens — safe under e5's 512-token window. Kannada
tokenizes denser in XLM-R's sentencepiece than English does. The eval spike must also measure this:
tokenize the longest Kannada-bearing chunks with the candidate model's tokenizer. Decision rule:

- if **< 5%** of chunks exceed 512 tokens → accept truncation on outliers, note it here;
- if **≥ 5%** → lower `chunk_size` to 900 **before** the re-index (chunk ids are
  `sha1(rel|hash|index)`, so the re-index rebuilds them anyway) and record the change.

## 4. The eval set (the gate — no switch without it)

Mirror `docs/ocr_pass_design.md`'s spike-first rollout: ~20 hand-labelled queries, each with a known
target file + page, split deliberately:

- **7 Kannada questions** about orders with Kannada sections (the broken case),
- **7 English questions** whose answers live in mixed or Kannada-bearing chunks (cross-lingual),
- **6 control questions** with purely-English targets (the regression risk).

Procedure: run each query through `python scripts/query_cli.py "<q>" --no-llm --json`, and score
**recall@6** (6 = `llm_top_k`) and MRR against the hand labels. Baseline MiniLM numbers go into §8
*before* the switch; e5-small numbers after.

Acceptance thresholds:

| Gate | Requirement |
|------|-------------|
| Kannada subsets | recall@6 at least **+20 points** over the MiniLM baseline |
| English control subset | no regression worse than **−2 points** |
| Token window | §3.4's rule satisfied |
| Overall | If `small` fails the Kannada gate → try `multilingual-e5-base` before escalating to BGE-M3 / nomic-v2 |

The eval queries and labels are committed alongside the results (a JSON file under `docs/`), so the
next embedder candidate is scored against the same yardstick.

## 5. Re-index procedure

Run from the project root, D: drive mounted; the run is resumable (the manifest is re-saved every
`batch_size` changes) and the OCR cache makes the OCR step free — the re-index pays only for
extraction (cached nowhere, ~75 min worst case per the corpus measurements) and embedding
(minutes-to-tens-of-minutes for 37k chunks on CPU; not yet measured — record the actual in §8).

1. **Baseline**: run the eval set on MiniLM; fill §8's baseline column. Label any query that
   already fails (target file not indexed, moved, etc.) so it does not pollute the comparison.
2. **Config edit** (one commit, with the code changes from §3):
   `embedding_model: "multilingual-e5-small"`, `embedding_query_prefix: "query: "`,
   `embedding_passage_prefix: "passage: "`, `collection_name: "kerc_docs_multi"`.
3. **Re-index**: `python scripts/incremental_ingest.py`. The fingerprint guard-rail re-processes
   every indexed file (`re-embed: the embedding model changed`); chunk ids stay stable
   (`sha1(rel|hash|index)` unchanged), so there are no duplicate-chunk artifacts.
4. **Re-run the eval** against the new collection; fill §8; check the gates.
5. **Decide**: pass → update `docs/stack_choices.md` §3/§4 to record the switch and retire the
   "Kannada retrieval is broken" finding; fail → escalate per §4's table, which is another
   fingerprint change and the same procedure.
6. **Cleanup**: keep the old collection until the new one has survived a few days of real use, then
   delete it (`rm -rf kerch_db` after confirming nothing points at `kerc_docs`); it holds no unique
   data — everything derives from the manifest + corpus. Old chunks reference `collection_name` in
   no manifest field, so the manifest needs no migration.

**Rollback**: revert the §5.2 config edit and re-run. The rollback is safe but not free — the
fingerprint now differs again, so the corpus re-embeds a *second* time on the old model into the old
collection name. That is the accepted cost of keeping the fingerprint honest.

## 6. Risks and mitigations

| Risk | Mitigation |
|------|------------|
| e5-small is still weak on Kannada (XLM-R covers 100 languages, but low-resource quality varies) | The eval gate (§4) exists for exactly this; escalate `small` → `base` → BGE-M3/nomic-v2, same machinery |
| Prefixes swapped (query prefix on the corpus, or none at all) | Each call site passes its intent once (§3.1); a test asserts the two paths send different prefixes; the eval set catches the score drop |
| Fingerprint misses a prefix-only edit | Fingerprint covers model + passage prefix (§3.2); query-prefix-only drift is a scores problem, caught by the eval |
| Same-dimension silent mixing | The v3 guard-rail re-embeds on any fingerprint change; fresh `collection_name` on top (§3.3) |
| Kannada chunks truncate at 512 tokens | Measured in the spike with an explicit decision rule (§3.4) |
| Long CPU re-index blocks the machine | Resumable via manifest checkpoints; overnight run; `--ocr-limit` unnecessary because the OCR cache answers all OCR'd pages for free |
| Corpus edited mid-migration | The run is incremental — files processed before the edit keep valid chunk ids; edited files re-classify on the next run as usual |

## 7. Out of scope

- Hybrid/BM25 retrieval — its own plan (`docs/hybrid_retrieval_plan.md`), deliberately scheduled to
  ride the same re-index window so the corpus is re-embedded once, not twice.
- Cross-encoder re-ranking, query rewriting, hierarchical/parent-document retrieval.
- Any change to the vector DB, chunker, OCR pass or LLM wiring.
- The Gradio UI (`docs/gradio_ui_design.md`) — it reads the same collection and needs nothing from
  this migration.

## 8. Results (fill in at execution)

| Subset | MiniLM baseline recall@6 | e5-small recall@6 | Δ | Gate |
|--------|--------------------------|-------------------|---|------|
| Kannada (7) | — | — | — | — |
| Cross-lingual EN→mixed (7) | — | — | — | — |
| English control (6) | — | — | — | — |
| MRR (all 20) | — | — | — | — |
| Chunks > 512 tokens | — | — | — | — |
| Re-index wall time | — | — | — | — |

## 9. References

- Model card (prefix requirement, dims, context): huggingface.co/intfloat/multilingual-e5-small;
  language coverage from XLM-R: huggingface.co/intfloat/multilingual-e5-large ("100 languages from
  xlm-roberta").
- E5 technical report: arxiv.org/abs/2402.05672.
- Practitioner's guide to e5 (prefix semantics): pinecone.io/learn/the-practitioners-guide-to-e5.
- Kannada embedder comparison (nomic v2 / Arctic / BGE-M3): thejeshgn — "Embedding models for
  Kannada" (also in `docs/stack_choices.md` §6).
- The gap this closes: `docs/stack_choices.md` §3.1–§3.4.
