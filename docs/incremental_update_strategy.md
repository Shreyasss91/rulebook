# Incremental Update Strategy for KERC RAG Pipeline

## Problem Statement (User Question)

> After initial scan, extraction, OCR, chunking, and vectoring of `D:/Office PC/D DRIVE/KERC` is complete:
> 1. **Edit one of the files** — content changes
> 2. **Place a new file** in the folder
> 3. **Place a new subfolder with new files**
> 4. **Move a file from one subfolder to another**
>
> **What happens? How does the pipeline handle these incrementally?**

---

## Current Behavior (No Auto-Handling)

| Scenario | What Happens Now | Works? |
|----------|------------------|--------|
| **1. Edit a file** | Re-run picks it up (content changed → new signature) → treats as new file | ✅ Yes |
| **2. Add new file** | Next run picks it up (not in `done` set) → processes normally | ✅ Yes |
| **3. New subfolder + files** | Next run recurses into it → picks up files | ✅ Yes |
| **4. Move file** | **PROBLEM:** Old path in `done`, new path not seen → treated as NEW file + old entry orphaned in ignore list | ❌ **Breaks** |

### The Core Problem: Move Detection

```
Before: KERC/Regulations/Rule14.pdf (in done set, maybe in ignore list)
After:  KERC/Updated/Rule14.pdf (not in done set → NEW file)
Result: Duplicate not caught; old ignore entry stale; vector DB has duplicate chunks
```

---

## Solution: Manifest-Based Incremental Pipeline

### Manifest File: `docs/file_manifest.json`

```json
{
  "KERC/Regulations/Rule14.pdf": {
    "size": 1024000,
    "mtime": 1727289600,
    "content_hash": "a1b2c3d4e5f6",
    "signature": "sig123",
    "chunk_ids": ["chunk_001", "chunk_002", "chunk_003"],
    "last_processed": "2026-09-25T10:30:00Z",
    "status": "indexed"
  }
}
```

### Per-Run Algorithm

```python
# 1. Scan all files → current_manifest
current_manifest = scan_all_files(config["docs_source"])

# 2. Load previous manifest
previous_manifest = load_manifest("docs/file_manifest.json")

# 3. Compare and classify
changes = classify_changes(current_manifest, previous_manifest)

# 4. Process each change type
for change in changes:
    if change.type == "NEW":
        process_new_file(change.path)
    elif change.type == "MODIFIED":
        reprocess_file(change.path)
    elif change.type == "MOVED":
        update_path_mapping(change.old_path, change.new_path)
    elif change.type == "DELETED":
        remove_from_index(change.path)

# 5. Save updated manifest
save_manifest("docs/file_manifest.json", current_manifest)
```

### Change Classification Logic

| Change Type | Detection | Action |
|-------------|-----------|--------|
| **NEW** | Path in current, not in previous | Full pipeline: extract → OCR → chunk → embed → upsert |
| **MODIFIED** | Same path, different `content_hash` or `mtime` | Re-process: extract → OCR → chunk → embed → upsert (replace chunks) |
| **MOVED** | Same `content_hash`, different path | Update manifest path; update vector DB metadata (no re-embedding) |
| **DELETED** | Path in previous, not in current | Delete chunks from vector DB; remove from manifest |
| **UNCHANGED** | Same path, same hash, same mtime | Skip entirely |

---

## Pipeline Component Strategies

| Component | Incremental Strategy |
|-----------|---------------------|
| **Deduplication** | Manifest tracks signatures; moved files keep same signature |
| **OCR** | Skip if `mtime` unchanged + `has_text_layer: true` in manifest |
| **Chunking** | Re-chunk only NEW/MODIFIED; MOVED reuses existing chunks |
| **Embeddings** | Re-embed only new chunks (NEW/MODIFIED) |
| **Vector DB** | ChromaDB `upsert` by chunk_id; `delete` for DELETED |

---

## Vector DB Operations (ChromaDB)

```python
# NEW / MODIFIED: upsert chunks
collection.upsert(
    ids=chunk_ids,
    documents=chunks,
    embeddings=embeddings,
    metadatas=[{"source": path, "chunk": i, ...} for i in range(len(chunks))]
)

# DELETED: remove by source path
collection.delete(where={"source": old_path})

# MOVED: update metadata (optional, for citation accuracy)
collection.update(
    ids=chunk_ids,
    metadatas=[{"source": new_path, "chunk": i, ...} for i in range(len(chunks))]
)
```

---

## Implementation Options

### Option A: Custom Manifest Script (Full Control)
- Single Python script: `scripts/incremental_ingest.py`
- Reads `config.yaml` + `docs/file_manifest.json`
- Handles all 4 scenarios correctly
- Zero new dependencies

### Option B: LlamaIndex / LangChain (Less Code)
- Built-in `VectorStoreIndex` with docstore
- `docstore` tracks document hashes automatically
- `refresh_ref_docs()` handles NEW/MODIFIED/MOVED/DELETED
- Adds dependencies (llama-index, langchain)

### Option C: Hybrid
- Custom manifest for deduplication + file tracking
- LlamaIndex only for chunking/embedding/vector DB

---

## Recommended: Option A (Custom) for Your Case

**Reasons:**
1. You already have deduplication logic with ignore list
2. Need precise control over citations (source path, page numbers)
3. OCR fallback logic is custom
4. Lightweight, no framework lock-in
5. Easy to debug and extend

---

## Next Steps

1. Create `scripts/incremental_ingest.py` with manifest logic
2. Add `file_manifest.json` to `.gitignore` (local only, regenerated)
3. Integrate with existing deduplication (`deduplication_ignore_list.json`)
4. Test all 4 scenarios:
   - Edit file → re-index
   - Add file → index
   - Add subfolder → index
   - Move file → update path, no re-embedding
4. Schedule via cron or run manually after changes

---

## Config Addition (config.yaml)

```yaml
# Incremental update settings
manifest_path: "docs/file_manifest.json"
track_moves: true
track_deletions: true
```

---

*Generated from discussion on 2026-09-25. User asked about incremental handling after initial RAG pipeline completion.*