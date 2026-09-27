# Stack Choices — Vector DB and Embeddings (pros, cons)

Status: **analysis**. This closes the `stack choices(pros, cons) for VEctorDB, Embeddings` item in
`docs/deferred.txt`. It compares the options, records what the project runs today, and names the
trigger that would justify switching. It deliberately changes **no** code: a switch in either
component is a re-index, so it should be a decision, not a default.

Related: `docs/origin_doc.md` (the original stack sketch), `docs/kerc_folder_inventory.md` (corpus
measurements), `docs/incremental_update_strategy.md` (the manifest/collection), `docs/query_cli_design.md`
(how retrieval and citations consume this).

---

## 1. The decision drivers

The corpus sets the constraints more tightly than any benchmark:

| Driver | Value | Consequence |
|--------|-------|-------------|
| Privacy | Karnataka government regulatory documents, "local-first" by design | Cloud vector DBs and embedding APIs are out, not merely slower |
| Hardware | Windows, i5-13500T, **8 GB RAM**, no usable GPU (`docs/origin_doc.md`) | CPU-only; embedding throughput, not DB speed, is the scarce resource |
| Scale | **37,118 chunks** today; ~45–50k after the OCR pass fills 143 image-only files | Every shortlisted DB is over-specified; memory is not a real constraint |
| Language | **Bilingual English + Kannada** (KERC orders mix both; OCR runs `eng+kan`) | The embedder must be multilingual — this is the one hard requirement that the current stack fails |
| Citations | Chunks must keep `source`/`page`/`section` for page-exact answers | The store must support **metadata filtering** and per-record CRUD |
| Updates | Incremental: NEW/MODIFIED/MOVED/DELETED with deletes by `source` | The store must support upsert + delete-by-filter; a pure ANN library is not enough |

Sizing the memory argument, so it is not repeated: at 37k chunks a 384-dim float32 index is ~57 MB,
768-dim ~114 MB and 1024-dim ~152 MB before HNSW overhead — a few hundred MB even with 3–4× graph
overhead. A vendor's "~6 GB for 1,000,000 × 1536-dim vectors" number is ~27× larger than this corpus,
which is why it does not decide anything here.

A note on discovery tooling: the service catalog consulted for this analysis lists vector databases
as *managed cloud* products (Pinecone, Zilliz/Milvus Cloud, Weaviate Cloud, Chroma Cloud, turbopuffer,
Astra, Atlas). For a self-hosted, air-gapped requirement it returned **no** matching option — the
local-first constraint eliminates the whole hosted category before quality is even compared.

## 2. Vector DB

### 2.1 Options

| Option | Model | Metadata filter / CRUD | Hybrid (keyword) | Footprint | Verdict for this project |
|--------|-------|------------------------|------------------|-----------|--------------------------|
| **ChromaDB** *(current)* | Embedded (in-process), SQLite metadata + HNSW | ✅ `where`, `where_document $contains`, delete-by-filter, upsert | ⚠️ Basic; no native BM25 | In-RAM index, tiny at 37k | **Keep.** Already implemented and used by `query_cli.py` |
| **LanceDB** | Embedded, Lance columnar files, memory-mapped | ✅ SQL filtering (DataFusion), versioned tables | ✅ Native vector + Tantivy full-text | 12–18 GB per 1M×1536; far less here | **Best migration target** if/when hybrid search lands |
| **Qdrant** (self-hosted) | Separate server (HTTP/gRPC); Qdrant Edge is in-process | ✅ Richest filters | ✅ Sparse + dense | Server process | Strong, but a second process to run on a single-user laptop |
| **sqlite-vec** | Embedded, single file, SQL | ✅ SQL | Via FTS5 | Tiny | Appealing simplicity; younger, leaner ANN/filtering |
| **FAISS** | Library, not a database | ❌ Bring your own ids/metadata/CRUD | ❌ | Tiny | Rejected: no persistence or metadata to cite |
| **pgvector / Postgres**, **DuckDB VSS** | Embedded-ish / server | ✅ SQL | Postgres FTS / DuckDB FTS | Postgres = a service | Rejected: adds an RDBMS for no gain at this scale |
| **Milvus, Weaviate** | Distributed / server | ✅ | ✅ | Heavy | Rejected: infrastructure out of proportion to 37k chunks |
| **Hosted** (Pinecone, Zilliz, Weaviate Cloud, Chroma Cloud, turbopuffer, Atlas) | Managed | ✅ | ✅ | None local | **Rejected:** data leaves the machine and adds recurring cost |

### 2.2 Pros / cons of the realistic shortlist

**ChromaDB — current**
- ✅ Embedded (no server, no port), pure Python, `PersistentClient` survives restarts, works offline.
- ✅ The exact API this project already uses: `where` metadata filters, `$contains` substring search,
  `upsert`/`delete(where={"source": …})` for the incremental pipeline.
- ✅ Trivial at this scale, with first-class LangChain/LlamaIndex integrations if the stack grows.
- ❌ Single-writer in embedded mode (fine: one ingest process).
- ❌ Index lives in RAM (fine here; a caveat at 10⁷ vectors).
- ❌ Weak native full-text: the deferred BM25/hybrid milestone would be built by hand on top.
- ❌ Version drift: the installed wheel is old relative to current releases; telemetry banners are
  noisy. Neither affects correctness, but pin deliberately.

**LanceDB**
- ✅ Embedded like Chroma, but disk-first with memory-mapped Lance files — a better fit if RAM ever
  matters.
- ✅ **Native hybrid retrieval** (dense + Tantivy full-text + SQL filters in one query): exactly the
  "combine vector + keyword for exact rule numbers" goal in `docs/origin_doc.md`.
- ✅ Arrow ecosystem, automatic table versioning.
- ❌ Optimistic concurrency: concurrent writers must handle commit conflicts (single ingest process
  makes this a non-issue today).
- ❌ Younger API surface; a migration means rewriting `open_collection` + the delete/upsert calls.
- ❌ An extra dependency chain (Arrow/Lance) in a deliberately lean project.

**Qdrant (server) / Qdrant Edge**
- ✅ Best-in-class filtering and payloads, scalar/binary quantization, snapshots; hybrid sparse+dense.
- ✅ Qdrant Edge (GA 2026) brings the engine in-process for embedded use.
- ❌ Server mode is a separate process/failure domain on a personal Windows machine; Edge is new.
- ❌ More moving parts than a 37k-chunk, single-user corpus warrants.

### 2.3 Recommendation — Vector DB

**Keep ChromaDB.** It satisfies every hard requirement (embedded, offline, metadata filters, CRUD),
it is already implemented and tested, and at ~37k chunks it sits ~27× below the 1M-vector point
where its RAM/single-writer caveats start to matter. The only declared gap is native keyword search,
and that is a *retrieval-feature* reason to migrate, not a performance one.

**Revisit when any of these becomes true** (the trigger list):
1. Hybrid BM25 + vector search is wanted and hand-rolling it on Chroma proves painful → **LanceDB**.
2. The corpus grows past ~1M chunks or the index no longer fits comfortably in RAM → **LanceDB** or
   **Qdrant**.
3. Multiple concurrent writers (e.g. a scheduled ingest plus a query service writing) → **Qdrant**.
4. A migration of the embedding model is already forcing a re-index → the cheapest moment to also
   switch the store, if either of the above is close.

## 3. Embeddings

### 3.1 The finding that matters: the corpus is bilingual, the model is not

The project embeds with **`all-MiniLM-L6-v2`**, an English-only model. Every Kannada passage in the
corpus (and KERC orders are routinely mixed `eng+kan`, which is why the OCR pass refuses an
English-only language fallback) is therefore embedded into a space it was never trained for: Kannada
chunks retrieve poorly regardless of how good the chunking and citations are. The OCR decision to
treat `eng+kan` strictly is undermined on the *retrieval* side. This is the single most consequential
stack gap, and it is invisible in English-only spot checks.

### 3.2 Options

| Model | Params / dims | Context | Languages | Notes | Fit |
|-------|---------------|---------|-----------|-------|-----|
| **all-MiniLM-L6-v2** *(current)* | 22M / 384 | ~256 tokens | **English only** | Fast, tiny, ubiquitous. 256-token cap truncates ~1200-char legal chunks | Baseline; **English-only is the blocker** |
| **multilingual-e5-small / base / large** | 118M / 384 · 278M / 768 · 560M / 1024 | 512 | 100+ | Asymmetric `query:`/`passage:` prefixes; `e5` objectives win retrieval benchmarks | **Strong low-risk upgrade** (`small`/`base` CPU-friendly) |
| **BGE-M3** | 568M / 1024 | 8K | 100+ | Dense + **sparse + multi-vector** in one model (native hybrid); Apache 2.0 | **Best all-round local pick**; heavier on CPU |
| **Nomic Embed v2 (MoE)** | MoE / 768 (Matryoshka) | 8K | 100+ | Independently reported as one of the strongest open models for **Kannada** | Excellent when Kannada quality leads |
| **snowflake-arctic-embed-l-v2.0** | 568M / 1024 | 8K | 100+ | Also reported strong for Kannada; long context | Contender for the multilingual pass |
| **EmbeddingGemma-300M** | 300M / 768 (Matryoshka) | 2K | 100+ | Efficient, Matryoshka-truncatable, good quality/size | Great 8 GB-RAM compromise |
| **Qwen3-Embedding-0.6B / 4B / 8B** | 0.6B / 1024 … 8B | 32K | 100+ | Top accuracy; 0.6B is the CPU-practical one | 0.6B viable; 4B+ out of RAM budget at index time |
| **jina-embeddings-v3 / v4** | ~570M / 1024 | 8K | 100+ | Long context, multilingual; **check the weights license** (v3 is non-commercial) | Only if the license fits |
| **LaBSE** | 471M / 768 | 512 | 109 | Widely cited, but benchmarks show **poor retrieval** (built for translation similarity, not search) | **Not recommended** for retrieval |
| **APIs** (OpenAI `text-embedding-3-large`, Cohere, Voyage) | — | — | 100+ | Best raw Kannada quality in independent tests | **Rejected:** data leaves the machine + recurring cost |

Dimensions and context lengths move with model revisions; treat the table as a shortlist to verify
against the current model card before switching, not as a spec sheet.

### 3.3 Pros / cons of the realistic shortlist

**Stay on `all-MiniLM-L6-v2`**
- ✅ No re-index; already baked into `config.yaml`, the manifest and the 37k embedded chunks.
- ✅ Smallest/fastest CPU footprint; plenty accurate for English-only questions.
- ❌ **Kannada retrieval is effectively broken** — the corpus's defining feature.
- ❌ 256-token window invites truncation on 1200-char chunks.

**Upgrade to a multilingual model (recommended)**
- ✅ Retrieval works across the bilingual corpus; often better English retrieval too.
- ✅ `multilingual-e5` (small/base) and `EmbeddingGemma-300M` stay within an 8 GB CPU budget.
- ✅ Matryoshka models (`nomic v2`, `EmbeddingGemma`) can trade dimensions for speed/latency.
- ✅ BGE-M3 additionally unlocks the **hybrid** milestone by supplying sparse vectors.
- ❌ **Requires a full re-index**: a different model produces a different vector space *and* a
  different dimension, which a Chroma collection cannot change in place.
- ❌ Slower CPU indexing (roughly minutes-to-tens-of-minutes over 37k chunks, model-dependent) and a
  larger on-disk cache.
- ❌ Multilingual models ask for input prefixes (`query:` / `passage:`) — the ingest and query paths
  must both apply the same convention or scores degrade quietly.

**Hybrid/multi-vector (BGE-M3)** is the strongest fit but is the biggest change: it also implies
sparse-index storage and a fusion step (RRF), which is the retrieval milestone, not a config swap.

### 3.4 Recommendation — Embeddings

**Treat the multilingual upgrade as the next substantive milestone, not a config edit.** The project
should pick between:

- **`multilingual-e5-small`** — minimal-risk first step: 384-dim like today, 512-token window, well
  documented; proves the Kannada gain cheaply; and
- **`BGE-M3`** (or `nomic-embed-text-v2-moe` / `EmbeddingGemma-300M`) — the target state, chosen after
  a small Kannada retrieval spike on ~20 real queries, mirroring how `docs/ocr_pass_design.md` keeps
  a spike as rollout step 1.

**The re-index trap (a real gap in the current design).** `incremental_ingest.py` keys change
detection on the file's `content_hash`; nothing in the manifest records **which embedding model**
produced the chunks. Changing `embedding_model` in `config.yaml` therefore leaves every file
`UNCHANGED` and **nothing is re-embedded** — the query CLI would silently mix a new query embedder
with old-model vectors. Before any switch, either:

1. add an `embedding_fingerprint` (model name + dimensions) to the manifest and re-process when it
   changes — the same mechanism `ocr_fingerprint` already uses; or
2. bump `collection_name` (e.g. `kerc_docs_multi`) and run a clean full ingest into the new
   collection, keeping the old one intact for comparison.

Option 2 is the safer first migration; option 1 is the durable fix and is recommended regardless of
which model is chosen.

## 4. Decision summary

| Component | Now | Recommendation | Changes when |
|-----------|-----|----------------|--------------|
| Vector DB | ChromaDB (embedded, HNSW, metadata filters) | **Keep** | Hybrid/keyword search or >1M chunks → LanceDB; multi-writer → Qdrant |
| Embeddings | `all-MiniLM-L6-v2` (English) | **Upgrade to a multilingual model** (`multilingual-e5-small` first, `BGE-M3`-class target) | As soon as a Kannada retrieval spike confirms the gain; pair with a full re-index |
| Guard-rail | manifest has no embedding fingerprint | Add `embedding_fingerprint`, or migrate via a new `collection_name` | Before the embedding switch |

Neither change touches a contract the query layer depends on (`source`/`page`/`section` metadata),
so citations survive either migration — only the vector space is rebuilt.

## 5. Similar analysis for the other stack entries

Brief, because each is already decided in code or a design doc:

| Entry | Chosen | Why / where it is recorded | Open question |
|-------|--------|----------------------------|---------------|
| PDF extraction | `pdfplumber` | Handles tables/structure; measured in `docs/incremental_update_strategy.md` | `pymupdf` is faster but AGPL — a licensing, not quality, call |
| OCR | Tesseract + Novita DeepSeek OCR 2 hybrid, page-level | `docs/ocr_pass_design.md`, incl. the decision log and cost controls | Confirm the Novita model string in the spike (§11 step 1) |
| Chunking | Custom legal-aware, page-bounded | `docs/incremental_update_strategy.md`; keeps rule/section headings and exact pages | 1200-char target vs. embedder token window (see §3) |
| Vector DB | ChromaDB | §2 | Hybrid/keyword support |
| Embeddings | `all-MiniLM-L6-v2` | §3 | Multilingual upgrade |
| LLM | Ollama local (default), Claude optional | `docs/query_cli_design.md` | Model size for 8 GB; bilingual answer quality in Kannada |
| Orchestration | **None — plain Python** (no LangChain/LlamaIndex) | Keeps the pipeline debuggable and dependency-light; the original sketch suggested a framework | Revisit only if agentic/multi-step retrieval is added |

## 6. References

- Embedded vector DB comparison (ChromaDB / LanceDB / Qdrant Edge, RAM figures): actian.com —
  "Comparing Embedded Vector Databases in 2026" (2026-09-04).
- Local embedding model ranking (BGE-M3, Qwen3-Embedding, Nomic v2, EmbeddingGemma, MiniLM):
  local-ai-zone — "Top Embedding Models 2026" (2026-08-04).
- Multilingual retrieval benchmark (e5 strength; LaBSE poor; nomic v1.5 CJK gaps): aimultiple —
  "Top 10 Multilingual Embedding Models for RAG" (2026-02-20).
- Kannada embedding quality (nomic v2 MoE / Arctic Embed 2 / BGE-M3 / OpenAI): thejeshgn —
  "Embedding models for Kannada" (2025-06-18).
- Original project stack and hardware: `docs/origin_doc.md`.
