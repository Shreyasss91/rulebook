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

## Implementation Status: Done (Option A)

`scripts/incremental_ingest.py` implements everything above. Deviations from the
sketch in this document, and why:

| Sketch | Implementation | Why |
|--------|----------------|-----|
| `chunk_ids: [...]` stored in the manifest | `chunk_count` only; ids derived as `sha1("<rel_path>\|<content_hash>\|<index>")` | A full corpus would put ~190k ids in the manifest; ids stay deterministic and the collection is pruned by metadata filters instead |
| Hash only for change detection | size+mtime fast path, SHA256 when either differs, `--full-hash` to override | Avoids re-reading 25k pages on every run |
| OCR skips on `mtime` + `has_text_layer` | PDFs below `ocr_min_chars_per_page` are marked `needs_ocr` and retried once `OCR_AVAILABLE` is flipped in the script | OCR is not built yet, so retrying today would just repeat the same failure |
| `process_new_file` etc. as separate steps | Single `process_new_or_modified` that degrades to `pending_embedding` when sentence-transformers/chromadb are absent | Lets the manifest stage be used before the RAG stack is installed |
| Move = metadata update | Same, plus: a move of a file that was never indexed is reprocessed instead | Re-labelling zero chunks would silently lose the file |

Ignore-list entries are matched on the path relative to their source root (the same
convention `create_deduplication_ignore_list_v2.py` writes). The dedup script emits Windows
separators (`str(relative_to(source))`), so separators are normalised on load; absolute entries
match by their normalised absolute path. With more than one entry in `docs_source`, an entry can
therefore match a file in each source — keep ignore-list paths unique, or run one source at a time.

### Edge cases beyond the original four scenarios

These were not in the first sketch, but each one presents itself as a mass change if ignored:

| Situation | Behaviour |
|-----------|-----------|
| Source unreachable (unmounted `D:`, renamed folder) | Previous entries for that root are kept untouched; nothing is reported as DELETED, so an offline drive cannot empty the collection |
| Overlapping/nested `docs_source` entries | A file reachable through two roots is scanned once (under the outer root) and the nesting is warned about |
| File locked / permission denied | `error` status with the reason, run continues; retried up to `max_retries`, then parked for `--strict` |
| Zero-byte file, blank text file | `empty` (not `needs_ocr` — there is nothing to OCR) |
| Image-only or low-text PDF | `needs_ocr`, with `pages_without_text` recorded for partially scanned files |
| Touched but unchanged (mtime only) | UNCHANGED; the new mtime is stored, no re-embedding |
| Case-only rename on Windows | MOVED via content hash, no re-embedding |
| Renamed **and** edited | Content hash differs but the dedup signature matches → reported as a hint; the old chunks need a human decision instead of being dropped automatically |
| Duplicate content not in the ignore list | Reported at the end of the run, because each copy is indexed under its own id |
| Stale ignore-list entries | Counted at the end of the run so the list can be regenerated after large moves |
| Run interrupted (Ctrl-C, crash) | The manifest is re-saved every `batch_size` changes, so completed work is not repeated |
| Manifest written by a newer script | Refused via `schema_version` instead of being silently re-processed |

## Next Steps

1. ~~Create `scripts/incremental_ingest.py` with manifest logic~~ ✅
2. ~~Add `file_manifest.json` to `.gitignore` (local only, regenerated)~~ ✅
3. ~~Integrate with existing deduplication (`deduplication_ignore_list.json`)~~ ✅
4. ~~Test all 4 scenarios~~ ✅ verified on a scratch corpus: add, add-subfolder, edit, move, delete, plus rerun idempotency and dependency-less runs
5. Add xlsx/xls/csv extraction (currently `unsupported`) and the OCR pipeline (`needs_ocr`)
6. Schedule via cron (`--strict` exits 2 while files are blocked) or run manually after changes
7. Consider signature-based move detection for renames that also edit content — today they are
   reported as a hint only, because a wrong automatic merge would silently drop a document

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