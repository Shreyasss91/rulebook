# Approach 1: Local RAG Pipeline for KERC Rules Query System

## Problem
Build an LLM interface to query professional rules/regulations from `D:\Office PC\D DRIVE\KERC` (50+ PDFs) that returns answers with cited sources.

## Recommended Architecture (with OCR)

```
PDFs → Text Extraction (pdfplumber) → OCR Fallback (Tesseract/ocrmypdf) → Chunking → Embeddings → Vector DB → LLM Query → Cited Answers
```

## Stack (Local-First, Free)

| Component | Recommendation | Why |
|-----------|----------------|-----|
| **Vector DB** | ChromaDB or Qdrant (local) | Free, fast, persistent, Python-native |
| **Embeddings** | sentence-transformers (`all-MiniLM-L6-v2`) | Free, runs locally, good for legal text |
| **LLM** | Ollama (local) or Claude API | Ollama = free/private; Claude = better reasoning |
| **PDF Extraction** | pdfplumber or pymupdf (fitz) | Handles tables, structure better than PyPDF2 |
| **OCR Engine** | **Tesseract** (via `pytesseract`) or **ocrmypdf** | Best open-source OCR; ocrmypdf adds searchable text layer to PDF |
| **Framework** | LangChain or LlamaIndex | Orchestrates the pipeline cleanly |

### OCR Integration Strategy

```python
import pdfplumber
import pytesseract
from pdf2image import convert_from_path
from pathlib import Path

def extract_with_ocr_fallback(pdf_path):
    """Extract text; if page returns empty/short, run OCR."""
    full_text = []
    with pdfplumber.open(pdf_path) as pdf:
        for i, page in enumerate(pdf.pages):
            text = page.extract_text() or ""
            if len(text.strip()) < 50:  # Likely scanned page
                # Convert page to image, run OCR
                images = convert_from_path(pdf_path, first_page=i+1, last_page=i+1)
                text = pytesseract.image_to_string(images[0])
            full_text.append(f"[Page {i+1}]\n{text}")
    return "\n\n".join(full_text)
```

**Install OCR deps:**
```bash
# Windows: Install Tesseract from https://github.com/UB-Mannheim/tesseract/wiki
# Then:
pip install pytesseract pdf2image ocrmypdf
# ocrmypdf also: pip install ocrmypdf && ocrmypdf --install-deps
```

---

## OCR Engine Comparison: Tesseract vs DeepSeek OCR

### Quick Comparison

| Aspect | **Tesseract (v5.x)** | **DeepSeek OCR** |
|--------|---------------------|------------------|
| **Type** | Traditional OCR engine (open-source) | LLM-based vision model (API) |
| **Architecture** | LSTM + character classification | Multimodal transformer |
| **License** | Apache 2.0 (free, commercial OK) | Proprietary (API only) |
| **Cost** | Free (local compute only) | Pay-per-call (~$0.001-0.01/page) |
| **Privacy** | 100% local | Data sent to DeepSeek servers |

### Accuracy by Document Type

| Document Type | Tesseract | DeepSeek OCR |
|---------------|-----------|--------------|
| Clean printed text | ★★★★★ | ★★★★★ |
| Scanned legal/regulatory PDFs | ★★★★☆ | ★★★★★ |
| Tables/structured data | ★★☆☆☆ (needs post-processing) | ★★★★☆ (understands structure) |
| Handwriting | ★★☆☆☆ | ★★★★☆ |
| Low-quality/noisy scans | ★★☆☆☆ | ★★★★☆ |
| Multi-column layouts | ★★★☆☆ | ★★★★★ |
| Mathematical formulas | ★★☆☆☆ | ★★★★☆ |

### For KERC Regulatory PDFs Specifically

| Factor | Tesseract | DeepSeek OCR |
|--------|-----------|--------------|
| **Scanned government orders** | Good with 300+ DPI | Excellent even at 150 DPI |
| **Tables in tariff orders** | Poor (needs Camelot/Tabula) | Good (outputs markdown/JSON) |
| **Kannada/English mixed** | Needs trained model | Handles natively |
| **Section/Rule numbering** | Preserves but no understanding | Understands hierarchy |
| **Batch processing 50+ PDFs** | Fast local, parallelizable | API rate limits, cost adds up |

### Recommended: Hybrid Approach (Best of Both)

```python
# 1. First pass: ocrmypdf (Tesseract) on all PDFs
#    ocrmypdf --language eng+kan input.pdf output_searchable.pdf

# 2. Flag low-confidence pages
#    ocrmypdf --output-type pdf --pdf-renderer hocr ...

# 3. Re-process flagged pages with DeepSeek OCR API
#    Only ~5-10% of pages typically need this
```

### Cost Estimate for 50 PDFs

| Approach | Est. Cost | Time |
|----------|-----------|------|
| Tesseract only (local) | $0 | 10-30 min |
| DeepSeek OCR only (API) | ~$5-20 | Rate-limited |
| **Hybrid (90% Tesseract + 10% DeepSeek)** | **~$1-3** | **15-45 min** |

### Decision

| Your Situation | Use |
|----------------|-----|
| **Bulk processing, privacy, free** | Tesseract via `ocrmypdf` |
| **Difficult pages Tesseract fails on** | DeepSeek OCR (selective) |
| **Need structured table output (markdown/JSON)** | DeepSeek OCR |
| **Handwritten marginalia in regulations** | DeepSeek OCR |

**Start with Tesseract/ocrmypdf for the full corpus.** It makes PDFs searchable permanently, handles your volume for free, keeps data local. Only reach for DeepSeek OCR on specific problematic pages.

---

## DeepSeek OCR Local Inference — Hardware Assessment

### Your PC Specs
| Component | Spec |
|-----------|------|
| **CPU** | Intel Core i5-13500T (14 cores, 20 threads, 1.6–4.6 GHz) |
| **RAM** | **8 GB** total (≈7.7 GB usable) |
| **GPU** | **Intel UHD Graphics 770** (integrated, **2 GB shared VRAM**) |
| **OS** | Windows 10 Pro |

### DeepSeek OCR Models (from `deepseek-ai/DeepSeek-OCR` & `DeepSeek-OCR-2`)
| Repo | Likely Base Model | Est. Parameters | Open Weights |
|------|-------------------|-----------------|--------------|
| `DeepSeek-OCR` | DeepSeek-VL-1.3B or 7B | 1.3B–7B | ✅ Yes (Apache 2.0 / MIT) |
| `DeepSeek-OCR-2` | Updated VL model (likely 7B+) | 7B+ | ✅ Yes |

**Both CAN run locally** — cost = $0 after download. But hardware constraints apply.

### VRAM Requirements (4-bit Quantization)
| Model Size | Min VRAM (4-bit) | Min VRAM (8-bit) | Your GPU (2 GB) |
|------------|------------------|------------------|-----------------|
| 1.3B | ~1.5 GB | ~2.5 GB | ⚠️ Barely (4-bit only) |
| 7B | ~5 GB | ~8 GB | ❌ No |
| 14B+ | ~10 GB | ~16 GB | ❌ No |

### System RAM Requirements (CPU Offload via llama.cpp)
| Model | CPU Offload RAM | Your 8 GB RAM |
|-------|-----------------|---------------|
| 1.3B (4-bit) | ~2 GB | ✅ Possible |
| 7B (4-bit) | ~5 GB | ⚠️ Tight (OS + app + model) |
| 7B (8-bit) | ~8 GB | ❌ No |

### Feasibility Verdict for Your Hardware
| Model | Feasibility | Expected Speed |
|-------|-------------|----------------|
| **DeepSeek-OCR 1.3B (4-bit, CPU)** | ✅ **Possible** | ~5-10 sec/page |
| DeepSeek-OCR 7B (4-bit, CPU) | ⚠️ Marginal (OOM risk) | ~30-60 sec/page |
| DeepSeek-OCR-2 (likely 7B+) | ❌ **Not feasible** | N/A |
| Any model on iGPU (2 GB VRAM) | ❌ **Not feasible** | N/A |

### Practical Options for You

| Option | Description | Cost | Speed | Quality |
|--------|-------------|------|-------|---------|
| **1. Local 1.3B (CPU, llama.cpp)** | Run quantized 1.3B model on CPU | $0 | ~5-10 sec/page | Lower than 7B |
| **2. Cloud API (DeepSeek/OpenRouter)** | Pay per page | ~$0.001-0.01/page | 1-2 sec/page | Best (full 7B+) |
| **3. Hybrid (Recommended)** | Tesseract for 90%, API for 10% | ~$0.10-0.50 total | 15 min total | Best of both |

### Why NOT Run DeepSeek OCR Locally on This Hardware
1. **8 GB RAM is insufficient** for 7B+ models that make DeepSeek OCR worth using over Tesseract
2. **2 GB iGPU VRAM** cannot run any vision-language model (need 8+ GB VRAM)
3. **1.3B variant** (if available as GGUF) would be slower AND less accurate than Tesseract for clean scans
4. **CPU inference** on 1.3B: ~5-10 sec/page vs Tesseract's ~0.5 sec/page

### Updated Recommendation

**Stick with the hybrid approach:**
1. `ocrmypdf` (Tesseract) on full corpus — free, fast, makes PDFs searchable permanently
2. DeepSeek **API** only for problematic pages — pennies, high quality, no hardware constraints

---

## Novita.ai DeepSeek OCR 2 — Actual API Pricing

**Source:** https://novita.ai/models/model-detail/deepseek-deepseek-ocr-2?from=pricing

### Pricing
| Metric | Cost |
|--------|------|
| **Input tokens** | $0.03 / 1M tokens |
| **Output tokens** | $0.03 / 1M tokens |

### Cost Per Page Calculation

| Component | Est. Tokens |
|-----------|-------------|
| Image (1024×1024 vision patches) | ~576–1,024 |
| Prompt | ~50–100 |
| Output text (full page) | ~500–2,000 |
| **Total per page** | **~1,100–3,100 tokens** |

```
$0.03 / 1,000,000 = $0.00000003 per token

Low (1,100 tokens):  1,100 × $0.00000003 = $0.000033 ≈ $0.00003/page
High (3,100 tokens): 3,100 × $0.00000003 = $0.000093 ≈ $0.00009/page
```

**≈ $0.00003–0.0001 per page** (3–10 cents per 1,000 pages)

### Your 50 PDFs — Real Cost Estimates

| Scenario | Pages | Total Cost |
|----------|-------|------------|
| 50 PDFs × 10 pages | 500 | **$0.015–0.05** |
| 50 PDFs × 20 pages | 1,000 | **$0.03–0.10** |
| Hybrid (10% via API = 50 pages) | 50 | **$0.0015–0.005** |
| **All pages via Novita.ai** | 500–1,000 | **$0.015–0.10** |

### Price Comparison

| Provider | Cost/Page | 500 Pages |
|----------|-----------|-----------|
| **Novita.ai DeepSeek OCR 2** | **$0.00003–0.0001** | **$0.015–0.05** |
| Previous estimate (DeepSeek direct) | $0.001–0.01 | $0.50–5.00 |
| Tesseract (local) | $0 | $0 |

**Novita.ai is 10–100× cheaper than previous estimates!** At these prices, running ALL pages through the API is trivial cost.

### Updated Practical Options

| Option | Description | Cost (500 pages) | Speed | Quality |
|--------|-------------|------------------|-------|---------|
| **1. Tesseract only (local)** | ocrmypdf on all | $0 | 10–30 min | Good for clean scans |
| **2. Novita.ai DeepSeek OCR 2 (all pages)** | API for entire corpus | **$0.015–0.05** | 5–10 min | Best (7B+ model) |
| **3. Hybrid (Recommended)** | Tesseract 90% + Novita.ai 10% | **$0.0015–0.005** | 15 min | Best of both |

### New Recommendation

At **$0.00003–0.0001/page**, the API cost is negligible. You can now:

1. **Run Tesseract/ocrmypdf on all** — free, makes PDFs permanently searchable
2. **Optionally re-process ALL pages via Novita.ai** for $0.03–0.10 total — superior quality, structured output (markdown/JSON), handles tables/Kannada/handwriting
3. **Or hybrid** — Tesseract first, Novita.ai only for flagged pages ($0.001–0.005)

The hardware constraint (8 GB RAM, 2 GB VRAM) is now irrelevant for API usage. You get 7B+ model quality with zero local compute.

---

## Quick Start Prototype (Updated with OCR)

### requirements.txt
```
pdfplumber
pytesseract
pdf2image
ocrmypdf
sentence-transformers
chromadb
langchain
ollama
```

### rag_pipeline.py
```python
import pdfplumber
import pytesseract
from pdf2image import convert_from_path
from sentence_transformers import SentenceTransformer
import chromadb
from langchain.text_splitter import RecursiveCharacterTextSplitter
from pathlib import Path

# 1. Extract text with OCR fallback
def extract_pdfs(folder):
    docs = []
    for pdf_path in Path(folder).rglob("*.pdf"):
        text = extract_with_ocr_fallback(pdf_path)
        docs.append({"source": str(pdf_path), "text": text})
    return docs

def extract_with_ocr_fallback(pdf_path):
    full_text = []
    with pdfplumber.open(pdf_path) as pdf:
        for i, page in enumerate(pdf.pages):
            text = page.extract_text() or ""
            if len(text.strip()) < 50:  # Likely scanned page
                images = convert_from_path(pdf_path, first_page=i+1, last_page=i+1)
                text = pytesseract.image_to_string(images[0])
            full_text.append(f"[Page {i+1}]\n{text}")
    return "\n\n".join(full_text)

# 2. Chunk with overlap (preserves legal context)
splitter = RecursiveCharacterTextSplitter(chunk_size=1000, chunk_overlap=200)

# 3. Embed & store
model = SentenceTransformer("all-MiniLM-L6-v2")
client = chromadb.PersistentClient(path="./kerch_db")
collection = client.get_or_create_collection("kerch_rules")

def index_docs(docs):
    for doc in docs:
        chunks = splitter.split_text(doc["text"])
        embeddings = model.encode(chunks).tolist()
        ids = [f"{doc['source']}_chunk_{i}" for i in range(len(chunks))]
        metadatas = [{"source": doc["source"], "chunk": i} for i in range(len(chunks))]
        collection.add(ids=ids, documents=chunks, embeddings=embeddings, metadatas=metadatas)

# 4. Query with citations
def query(question, k=5):
    q_emb = model.encode(question).tolist()
    results = collection.query(query_embeddings=[q_emb], n_results=k)
    
    # Format context with citations
    context = ""
    for doc, meta in zip(results["documents"][0], results["metadatas"][0]):
        context += f"[Source: {meta['source']}]\n{doc}\n\n"
    
    # Prompt for LLM (Ollama or Claude)
    prompt = f"""Answer the question using ONLY the context below. 
    Cite sources like [Source: filename.pdf] after each claim.
    
    Context:
    {context}
    
    Question: {question}
    Answer:"""
    
    return prompt  # Send to LLM
```

---

## Key Features for Legal/Regulatory Use Case

1. **Source citations** — Each chunk stores `source` (filename + page number)
2. **Legal-aware chunking** — Preserve regulation/section boundaries
3. **Hybrid search** — Combine vector + keyword (BM25) for exact rule numbers
4. **Local-first** — Sensitive regulatory docs never leave your machine
5. **OCR fallback** — Handles scanned pages automatically

---

## Detailed Comparison: Option A (Custom Prototype) vs Option B (Existing Tools)

### Option A: Custom Python Prototype

| Aspect | Details |
|--------|---------|
| **Control** | ✅ Full control over chunking strategy, retrieval logic, prompt engineering |
| **OCR Integration** | ✅ Custom fallback logic per page; can pre-process with `ocrmypdf` to make PDFs searchable permanently |
| **Citation Format** | ✅ Exact format you want (page numbers, section refs, regulation numbers) |
| **Legal Chunking** | ✅ Can implement section-aware splitting (e.g., split on "Section X", "Rule Y", "Clause Z") |
| **Hybrid Search** | ✅ Easy to add BM25/keyword search alongside vector search for exact rule lookups |
| **Data Privacy** | ✅ 100% local — nothing leaves your machine |
| **Cost** | ✅ Free (only compute/electricity) |
| **Maintenance** | ❌ You own the code — updates, bug fixes, dependency management |
| **UI** | ❌ Need to build CLI/Gradio/Streamlit yourself (but simple) |
| **Time to First Query** | ~2-4 hours to build and test |
| **Scaling** | ❌ Manual effort to optimize for 1000+ docs |
| **Multi-user** | ❌ Single-user by default |

**Best for:** You want precise control over how legal documents are parsed, chunked, and cited; you have specific citation format requirements; you're comfortable with Python.

---

### Option B: Existing Tools (AnythingLLM / Kotaemon / PrivateGPT)

| Tool | Pros | Cons |
|------|------|------|
| **AnythingLLM** | • Desktop app, good UI • Built-in OCR (Tesseract) • Workspaces for different document sets • Local LLM via Ollama • Citations with page refs • Active development | • Less control over chunking strategy • Electron app (heavier) • OCR quality fixed • Workspace isolation can be confusing |
| **Kotaemon** | • Open-source, Gradio UI • RAG pipeline configurable • Supports hybrid search • Citations with source highlights • Docker deployment | • More complex setup • UI less polished • OCR support varies |
| **PrivateGPT** | • Fully local, privacy-first • Ingestion pipeline configurable • API + Gradio UI • Supports multiple LLMs | • Minimal UI • Less active development • OCR not built-in (need custom ingest) |

| Aspect | Option B (General) |
|--------|-------------------|
| **Setup Time** | ✅ 15-30 minutes (download, point to folder, query) |
| **OCR** | ⚠️ Built-in but less customizable; may miss edge cases |
| **Citation Quality** | ⚠️ Standard format; harder to customize for legal refs |
| **Chunking Control** | ⚠️ Configurable but within their framework limits |
| **Maintenance** | ✅ Community handles updates |
| **Multi-user** | ✅ AnythingLLM/Kotaemon support it |
| **Data Privacy** | ✅ All local options available |
| **Cost** | ✅ Free (self-hosted) |

---

## Decision Matrix

| Your Priority | Choose |
|---------------|--------|
| **Fastest time-to-value** | Option B → AnythingLLM |
| **Precise legal citation format (section/rule/page)** | Option A |
| **Heavy scanned PDFs needing custom OCR tuning** | Option A |
| **Want to iterate on retrieval prompts/chunking** | Option A |
| **Team/multi-user access** | Option B (AnythingLLM) |
| **Zero coding, just works** | Option B |
| **Learning/extensibility** | Option A |

---

## My Recommendation

**Start with Option B (AnythingLLM)** for immediate productivity:
1. Install AnythingLLM desktop
2. Point to `D:\Office PC\D DRIVE\KERC`
3. Enable OCR in settings
4. Test queries — if citations/chunking are "good enough," stop here

**Switch to Option A** if:
- AnythingLLM's citations miss page numbers or regulation references
- Scanned PDFs aren't OCR'd well enough
- You need section-aware chunking (e.g., "Rule 14(3)" stays together)
- You want to embed this in your own tool/workflow

**Hybrid approach:** Use AnythingLLM for exploration → build Option A for production use with your exact requirements.

---

**Next Step:** Want me to build the Option A prototype with OCR, legal-aware chunking, and a Gradio UI? Or help you set up AnythingLLM first?