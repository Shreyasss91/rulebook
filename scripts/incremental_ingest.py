#!/usr/bin/env python3
"""
Incremental ingestion for the KERC RAG pipeline.

Manifest-based change detection (Option A in docs/incremental_update_strategy.md):

    scan -> classify -> process each change -> save manifest

Every file under `docs_source` gets a manifest entry in `docs/file_manifest.json`
tracking size, mtime, SHA256 content hash, dedup signature, page count and
processing status. Comparing the current scan against the previous manifest
classifies each file as one of:

    NEW        first time seen           -> extract, chunk, embed, upsert
    MODIFIED   content hash changed      -> re-extract, re-chunk, replace chunks
    MOVED      same hash, new path       -> rewrite chunk metadata, no re-embedding
    DELETED    gone from the sources     -> delete chunks from the collection
    UNCHANGED  same hash (and status ok) -> skipped entirely

Chunk ids are derived deterministically as sha1("<rel_path>|<content_hash>|<i>"),
so the manifest only needs to store `chunk_count` instead of every id, and the
vector store is still pruned/updated by metadata filters.

Graceful degradation — the script is useful before the RAG stack is installed:

  * without sentence-transformers/chromadb, extraction, chunking and the
    manifest still run; those files are marked `pending_embedding` and are picked
    up automatically by the next run that has the dependencies.
  * PDFs with no usable text layer are marked `needs_ocr` and left alone until
    the OCR milestone lands (`--strict` turns both cases into a failure for
    unattended/cron runs).

Usage:
    python scripts/incremental_ingest.py --dry-run      # report changes, write nothing
    python scripts/incremental_ingest.py                # apply changes
    python scripts/incremental_ingest.py --full-hash    # re-hash every file
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import yaml

# Reuse the deduplication signature so the manifest and the ignore list always
# agree on what "the same document" means. Scripts live in one directory, so the
# import works whenever this file is run directly.
try:
    from create_deduplication_ignore_list_v2 import get_file_signature
except ImportError:  # pragma: no cover - only when the script is copied out alone
    get_file_signature = None

try:
    import chromadb
except ImportError:
    chromadb = None

try:
    from sentence_transformers import SentenceTransformer
except ImportError:
    SentenceTransformer = None

try:
    import pdfplumber
except ImportError:
    pdfplumber = None

try:
    from docx import Document
except ImportError:
    Document = None


SCHEMA_VERSION = 1
HASH_CHARS = 16

# Change types
NEW, MODIFIED, MOVED, DELETED, UNCHANGED = "NEW", "MODIFIED", "MOVED", "DELETED", "UNCHANGED"

# Processing statuses stored in the manifest
INDEXED = "indexed"
NEEDS_OCR = "needs_ocr"
PENDING_EMBEDDING = "pending_embedding"
UNSUPPORTED = "unsupported"
EMPTY = "empty"
ERROR = "error"

# Statuses that mean "not finished" - retried whenever the blocking dependency
# becomes available again.
RETRYABLE = {NEEDS_OCR, PENDING_EMBEDDING, ERROR}

# OCR is not implemented yet (see Next Milestones). Flip this to True when the OCR
# pipeline lands and files marked needs_ocr start being retried automatically.
OCR_AVAILABLE = False

# Manifest entry fields that survive from the previous run.
CARRIED_FIELDS = ("content_hash", "signature", "page_count", "has_text_layer",
                  "chunk_count", "status", "last_processed", "note")

# Block-level split points for legal text: rule/section headings, all-caps
# titles, and numbered clauses keep their own block so citations stay exact.
HEADING_RE = re.compile(
    r"^\s*(?:"
    r"(?i:(?:section|rule|regulation|clause|article|chapter|part|schedule|annexure|form|order|notification|circular)\b.{0,80})"
    r"|[A-Z][A-Z0-9 ,&()/.\-]{6,}"
    r"|\d+(?:\.\d+)*[.)]\s+\S.{5,}"
    r")\s*$"
)
SENTENCE_SPLIT_RE = re.compile(r"(?<=[.;:!?])\s+")


def log(message: str, verbose_only: bool = False, verbose: bool = False) -> None:
    if verbose_only and not verbose:
        return
    print(message, flush=True)


# --------------------------------------------------------------------------
# Config / manifest I/O
# --------------------------------------------------------------------------

def load_config(path: Path) -> dict:
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {path}")
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def load_manifest(path: Path) -> dict:
    """Load the previous manifest. Accepts the legacy flat {"path": {...}} shape."""
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    files = data.get("files", data)
    return files if isinstance(files, dict) else {}


def save_manifest(path: Path, files: dict, stats: dict) -> None:
    payload = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "stats": stats,
        "files": dict(sorted(files.items())),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    tmp.replace(path)  # atomic-ish: never leave a half-written manifest behind


def load_ignore_list(config: dict) -> set[str]:
    """Relative paths the dedup run decided to skip (same convention as the ignore list)."""
    path = Path(config.get("ignore_list_path", ""))
    if not path.exists():
        return set()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return set()
    return set(data) if isinstance(data, list) else set()


# --------------------------------------------------------------------------
# Scan
# --------------------------------------------------------------------------

def hash_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(chunk_size), b""):
            digest.update(block)
    return digest.hexdigest()[:HASH_CHARS]


def iter_source_files(sources: list[str], file_types: list[str]):
    """Yield (root, path) for every configured file type below each source."""
    extensions = {f".{ext.lower().lstrip('.')}" for ext in file_types}
    for source in sources:
        root = Path(source).expanduser()
        if not root.exists():
            print(f"Warning: source path does not exist, skipping: {root}", file=sys.stderr)
            continue
        for path in root.rglob("*"):
            if path.is_file() and path.suffix.lower() in extensions:
                yield root, path


def scan_sources(config: dict, previous: dict, full_hash: bool) -> tuple[dict, dict]:
    """
    Build the current manifest.

    Files whose size and mtime match the previous manifest are carried over
    without re-reading them; anything else gets a fresh content hash. Entries in
    the deduplication ignore list are matched on their path relative to their
    source root - the same convention `create_deduplication_ignore_list_v2.py`
    writes - and are left out of the manifest entirely.
    """
    sources = config["docs_source"]
    file_types = config.get("file_types", ["pdf"])
    batch_size = config.get("batch_size", 100)
    ignore = load_ignore_list(config)

    current: dict[str, dict] = {}
    counters = {"scanned": 0, "ignored": 0, "hashed": 0, "missing_deps": set()}

    for index, (root, path) in enumerate(iter_source_files(sources, file_types), start=1):
        rel = path.relative_to(root).as_posix()
        counters["scanned"] += 1

        if rel in ignore:
            counters["ignored"] += 1
            continue

        stat = path.stat()
        prev = previous.get(rel)

        entry = {
            "root": str(root),
            "size": stat.st_size,
            "mtime": stat.st_mtime,
        }
        if prev:
            # Carry previous results forward so unchanged files stay untouched and
            # unfinished ones keep the status the retry logic looks at.
            entry.update({key: prev.get(key) for key in CARRIED_FIELDS})

        unchanged_stat = prev is not None and not full_hash and (
            prev.get("size") == stat.st_size and prev.get("mtime") == stat.st_mtime
        )

        if unchanged_stat:
            current[rel] = entry
        else:
            entry["content_hash"] = hash_file(path)
            counters["hashed"] += 1
            if get_file_signature is not None:
                entry["signature"] = get_file_signature(path, config)
            current[rel] = entry

        if index % batch_size == 0:
            print(f"  scanned {index} files...", flush=True)

    return current, counters


# --------------------------------------------------------------------------
# Classification
# --------------------------------------------------------------------------

def needs_reprocessing(prev_entry: dict, can_embed: bool) -> str | None:
    """Return a reason to re-process a file whose content is unchanged, else None."""
    status = prev_entry.get("status")
    if status == NEEDS_OCR:
        return "retry: OCR available" if OCR_AVAILABLE else None
    if status in RETRYABLE and can_embed:
        return f"retry: previous run ended in {status}"
    return None


def classify(current: dict, previous: dict, config: dict, can_embed: bool) -> list[dict]:
    track_moves = config.get("track_moves", True)
    track_deletions = config.get("track_deletions", True)

    # Paths that vanished, grouped by content hash so a move can be recognised.
    vanished = {path: entry for path, entry in previous.items() if path not in current}
    by_hash: dict[str, list[str]] = defaultdict(list)
    for path, entry in vanished.items():
        by_hash[entry.get("content_hash")].append(path)

    changes: list[dict] = []

    for rel, entry in sorted(current.items()):
        prev = previous.get(rel)

        if prev is None:
            old_path = None
            if track_moves:
                candidates = [p for p in by_hash.get(entry.get("content_hash"), []) if p in vanished]
                if candidates:
                    old_path = sorted(candidates)[0]
                    vanished.pop(old_path)
            if old_path:
                changes.append({"type": MOVED, "path": rel, "old_path": old_path,
                                "entry": entry, "previous": previous[old_path]})
            else:
                changes.append({"type": NEW, "path": rel, "entry": entry})
            continue

        if prev.get("content_hash") != entry.get("content_hash"):
            changes.append({"type": MODIFIED, "path": rel, "entry": entry, "previous": prev})
            continue

        reason = needs_reprocessing(prev, can_embed)
        if reason:
            changes.append({"type": MODIFIED, "path": rel, "entry": entry, "previous": prev,
                            "reason": reason})
        else:
            changes.append({"type": UNCHANGED, "path": rel, "entry": entry, "previous": prev})

    for rel, entry in sorted(vanished.items()):
        if track_deletions:
            changes.append({"type": DELETED, "path": rel, "entry": entry, "previous": entry})
        else:
            changes.append({"type": UNCHANGED, "path": rel, "entry": entry, "previous": entry,
                            "keep": True, "reason": "deletions not tracked"})

    return changes


# --------------------------------------------------------------------------
# Extraction
# --------------------------------------------------------------------------

def extract_pages(path: Path, config: dict) -> tuple[list[dict] | None, str | None]:
    """
    Extract text per page. Returns (pages, error_note).

    `pages` is a list of {"page": int|None, "text": str}; page is None for formats
    without pages (docx/txt/md), where citations fall back to the file itself.
    None means the format is not supported yet.
    """
    suffix = path.suffix.lower()

    try:
        if suffix == ".pdf":
            if pdfplumber is None:
                return None, "pdfplumber not installed"
            pages = []
            with pdfplumber.open(path) as pdf:
                for number, page in enumerate(pdf.pages, start=1):
                    pages.append({"page": number, "text": page.extract_text() or ""})
            return pages, None

        if suffix == ".docx":
            if Document is None:
                return None, "python-docx not installed"
            document = Document(path)
            parts = [p.text for p in document.paragraphs]
            for table in document.tables:
                for row in table.rows:
                    parts.append(" | ".join(cell.text.strip() for cell in row.cells))
            return [{"page": None, "text": "\n".join(parts)}], None

        if suffix in {".txt", ".md"}:
            return [{"page": None, "text": path.read_text(encoding="utf-8", errors="ignore")}], None

    except Exception as exc:  # unreadable/encrypted/corrupt files must not kill the run
        return [], f"{type(exc).__name__}: {exc}"

    return None, f"{suffix} extraction not implemented"


def has_text_layer(pages: list[dict], config: dict) -> bool:
    if not pages:
        return False
    minimum = config.get("ocr_min_chars_per_page", 50)
    characters = sum(len(p["text"].strip()) for p in pages)
    return (characters / len(pages)) >= minimum


# --------------------------------------------------------------------------
# Chunking (legal-aware)
# --------------------------------------------------------------------------

def split_into_blocks(text: str) -> list[tuple[str | None, str]]:
    """Split text into (heading, body) blocks; a heading opens a new block."""
    blocks: list[tuple[str | None, str]] = []
    heading: str | None = None
    body: list[str] = []

    for line in text.splitlines():
        if line.strip() and HEADING_RE.match(line):
            if heading is not None or body:
                blocks.append((heading, "\n".join(body).strip()))
            heading = line.strip()[:120]
            body = [line]
        else:
            body.append(line)

    if heading is not None or body:
        blocks.append((heading, "\n".join(body).strip()))

    return [(h, b) for h, b in blocks if b]


def split_long_block(block: str, size: int) -> list[str]:
    if len(block) <= size:
        return [block]
    pieces: list[str] = []
    for sentence in SENTENCE_SPLIT_RE.split(block):
        if pieces and len(pieces[-1]) + len(sentence) + 1 <= size:
            pieces[-1] = f"{pieces[-1]} {sentence}"
        else:
            pieces.append(sentence)
    # Anything still oversized (a giant table row, no sentence breaks) is cut hard.
    final: list[str] = []
    for piece in pieces:
        if len(piece) <= size:
            final.append(piece)
        else:
            final.extend(piece[i:i + size] for i in range(0, len(piece), size))
    return final


def chunk_text(text: str, block_heading: str | None, config: dict) -> list[dict]:
    size = config.get("chunk_size", 1200)
    overlap = min(config.get("chunk_overlap", 200), size - 1)
    minimum = config.get("min_chunk_chars", 200)

    chunks: list[dict] = []
    buffer = ""
    section: str | None = block_heading

    for heading, body in split_into_blocks(text):
        for piece in split_long_block(body, size):
            if not buffer:
                buffer, section = piece, heading or block_heading
            elif len(buffer) + len(piece) + 1 <= size:
                buffer = f"{buffer}\n{piece}"
            else:
                chunks.append({"text": buffer.strip(), "section": section})
                buffer = f"{buffer[-overlap:]}\n{piece}" if overlap else piece
                section = heading or block_heading

    if buffer.strip():
        chunks.append({"text": buffer.strip(), "section": section})

    # Merge runt chunks (page footers, stray headings) into their neighbour so no
    # text is dropped just because it is short.
    merged: list[dict] = []
    for chunk in chunks:
        if merged and len(chunk["text"]) < minimum:
            merged[-1]["text"] = f"{merged[-1]['text']}\n{chunk['text']}"
        else:
            merged.append(chunk)

    return [c for c in merged if c["text"].strip()]


def build_chunks(pages: list[dict], config: dict) -> list[dict]:
    """Chunk each page separately so every chunk keeps an exact page citation."""
    chunks: list[dict] = []
    for page in pages:
        for chunk in chunk_text(page["text"], None, config):
            chunks.append({**chunk, "page": page["page"]})
    return chunks


def make_chunk_id(rel_path: str, content_hash: str, index: int) -> str:
    raw = f"{rel_path}|{content_hash}|{index}".encode()
    return hashlib.sha1(raw).hexdigest()[:20]


# --------------------------------------------------------------------------
# Embeddings + vector store
# --------------------------------------------------------------------------

class Embedder:
    """Lazy sentence-transformers wrapper - the model is only loaded when needed."""

    def __init__(self, config: dict) -> None:
        self.model_name = config.get("embedding_model", "all-MiniLM-L6-v2")
        self.batch_size = config.get("embedding_batch_size", 32)
        self._model = None

    @staticmethod
    def available() -> bool:
        return SentenceTransformer is not None

    def encode(self, texts: list[str]) -> list[list[float]]:
        if self._model is None:
            print(f"Loading embedding model: {self.model_name}", flush=True)
            self._model = SentenceTransformer(self.model_name)
        vectors = self._model.encode(texts, batch_size=self.batch_size, show_progress_bar=False)
        return [vector.tolist() for vector in vectors]


def open_collection(config: dict):
    if chromadb is None:
        return None
    # Telemetry off: cron runs should not phone home, and the client would
    # otherwise print capture errors on every command.
    settings = chromadb.config.Settings(anonymized_telemetry=False)
    client = chromadb.PersistentClient(path=config.get("chroma_path", "kerch_db"), settings=settings)
    return client.get_or_create_collection(
        name=config.get("collection_name", "kerc_docs"),
        metadata={"hnsw:space": "cosine"},
    )


def chunk_metadata(rel: str, root: str, chunk: dict, index: int, total: int,
                   content_hash: str, suffix: str) -> dict:
    return {
        "source": rel,
        "root": root,
        "chunk": index,
        "chunk_total": total,
        "page": chunk["page"] if chunk["page"] is not None else -1,
        "section": chunk.get("section") or "",
        "file_type": suffix.lstrip("."),
        "content_hash": content_hash,
        "ingested_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def index_chunks(collection, embedder: Embedder, rel: str, root: str, path: Path,
                 chunks: list[dict], content_hash: str) -> int:
    ids = [make_chunk_id(rel, content_hash, i) for i in range(len(chunks))]
    documents = [c["text"] for c in chunks]
    metadatas = [chunk_metadata(rel, root, c, i, len(chunks), content_hash, path.suffix)
                 for i, c in enumerate(chunks)]
    embeddings = embedder.encode(documents)

    # Replace rather than merge, so a longer previous version leaves no orphans.
    collection.delete(where={"source": rel})
    collection.upsert(ids=ids, documents=documents, embeddings=embeddings, metadatas=metadatas)
    return len(chunks)


def delete_chunks(collection, rel: str) -> None:
    collection.delete(where={"source": rel})


def move_chunks(collection, old_path: str, new_path: str, root: str) -> int:
    result = collection.get(where={"source": old_path}, include=["metadatas"])
    ids = result.get("ids") or []
    if not ids:
        return 0
    metadatas = []
    for meta in result.get("metadatas") or []:
        meta = dict(meta)
        meta["source"] = new_path
        meta["root"] = root
        metadatas.append(meta)
    collection.update(ids=ids, metadatas=metadatas)
    return len(ids)


# --------------------------------------------------------------------------
# Processing
# --------------------------------------------------------------------------

def process_new_or_modified(change: dict, config: dict, embedder: Embedder,
                            collection, can_embed: bool) -> tuple[str, int, str | None]:
    """Extract -> chunk -> embed -> upsert one NEW/MODIFIED file."""
    rel = change["path"]
    entry = change["entry"]
    path = Path(entry["root"]) / rel

    pages, error = extract_pages(path, config)
    if pages is None:
        return UNSUPPORTED, 0, error
    if error:
        return ERROR, 0, error
    if not pages or not has_text_layer(pages, config):
        return NEEDS_OCR, 0, "no usable text layer - OCR not implemented yet"

    chunks = build_chunks(pages, config)
    if not chunks:
        return EMPTY, 0, "extracted text produced no chunks"

    entry["page_count"] = len(pages)
    entry["has_text_layer"] = True
    entry["chunk_count"] = len(chunks)

    if not can_embed or collection is None:
        # Chunks are already built, so the next run with the deps installed only
        # has to embed them - the chunk_count is recorded as computed.
        return PENDING_EMBEDDING, len(chunks), "sentence-transformers/chromadb not installed"

    indexed = index_chunks(collection, embedder, rel, entry["root"], path, chunks,
                           entry["content_hash"])
    return INDEXED, indexed, None


def apply_changes(changes: list[dict], config: dict, previous: dict,
                  current: dict, dry_run: bool, can_embed: bool, verbose: bool) -> dict:
    stats = defaultdict(int)
    embedder = Embedder(config)
    collection = None if dry_run else open_collection(config)

    for change in changes:
        kind = change["type"]
        rel = change["path"]
        entry = current.get(rel) or change.get("entry") or {}

        # A file that moved but was never successfully indexed (needs_ocr,
        # pending_embedding, error) has no chunks to re-label, so it is
        # re-processed under its new path instead.
        if kind == MOVED:
            old_status = previous.get(change["old_path"], {}).get("status")
            if old_status is None or old_status in RETRYABLE:
                kind = MODIFIED
                change["type"] = kind
                change["reason"] = f"moved from {change['old_path']}, never indexed"
                if collection is not None:
                    delete_chunks(collection, change["old_path"])

        stats[kind] += 1

        if kind == UNCHANGED:
            log(f"  {UNCHANGED:<9} {rel}" + (f"  ({change.get('reason')})" if change.get("reason") else ""),
                verbose_only=True, verbose=verbose)
            # Kept when deletions are not tracked, so the entry (and its chunks)
            # survive until the file reappears.
            if change.get("keep"):
                current[rel] = entry
            continue

        if dry_run:
            # Planned actions are listed once, in the summary block below.
            continue

        now = datetime.now(timezone.utc).isoformat(timespec="seconds")

        if kind == DELETED:
            if collection is not None:
                delete_chunks(collection, rel)
            current.pop(rel, None)
            log(f"  {DELETED:<9} {rel}")
            continue

        if kind == MOVED:
            old_entry = previous[change["old_path"]]
            moved = move_chunks(collection, change["old_path"], rel, entry["root"]) if collection else 0
            # Nothing is re-processed, so the old path's results carry over.
            for field in CARRIED_FIELDS:
                if old_entry.get(field) is not None:
                    entry[field] = old_entry[field]
            entry["last_processed"] = now
            stats["chunks_moved"] += moved
            log(f"  {MOVED:<9} {rel}  (from {change['old_path']}, {moved} chunks re-labelled)")
            current[rel] = entry
            continue

        status, chunk_count, note = process_new_or_modified(
            {"path": rel, "entry": entry}, config, embedder, collection, can_embed)
        entry["status"] = status
        entry["last_processed"] = now
        entry["chunk_count"] = chunk_count
        if note:
            entry["note"] = note
        else:
            entry.pop("note", None)
        current[rel] = entry

        stats[status] += 1
        stats["chunks_indexed" if status == INDEXED else "chunks_pending"] += chunk_count
        detail = f", {chunk_count} chunks" if chunk_count else ""
        message = f"  {kind:<9} {rel} -> {status}{detail}"
        if note:
            message += f"  ({note})"
        log(message, verbose_only=(status == INDEXED), verbose=verbose)

    return stats


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Manifest-based incremental ingestion for the KERC RAG pipeline.")
    parser.add_argument("--config", default="config.yaml", help="config file (default: config.yaml)")
    parser.add_argument("--source", action="append", default=None,
                        help="override docs_source (repeatable); useful for tests")
    parser.add_argument("--manifest", default=None,
                        help="override manifest_path from config")
    parser.add_argument("--chroma-path", default=None,
                        help="override chroma_path from config")
    parser.add_argument("--dry-run", action="store_true",
                        help="classify and report changes without writing anything")
    parser.add_argument("--full-hash", action="store_true",
                        help="re-hash every file instead of trusting size+mtime")
    parser.add_argument("--strict", action="store_true",
                        help="exit non-zero if any file is left pending (for cron)")
    parser.add_argument("-v", "--verbose", action="store_true", help="log every file")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    config = load_config(Path(args.config))

    if args.source:
        config["docs_source"] = args.source
    if args.chroma_path:
        config["chroma_path"] = args.chroma_path
    manifest_path = Path(args.manifest or config.get("manifest_path", "docs/file_manifest.json"))
    sources = [str(Path(s).expanduser()) for s in config["docs_source"]]

    previous = load_manifest(manifest_path)
    current, scan_stats = scan_sources(config, previous, args.full_hash)

    can_embed = Embedder.available() and chromadb is not None

    changes = classify(current, previous, config, can_embed)
    stats = apply_changes(changes, config, previous, current, args.dry_run, can_embed, args.verbose)

    if not args.dry_run:
        save_manifest(manifest_path, current, {"scanned": scan_stats["scanned"],
                                              "ignored": scan_stats["ignored"]})

    counts = {kind: sum(1 for c in changes if c["type"] == kind)
              for kind in (NEW, MODIFIED, MOVED, DELETED, UNCHANGED)}
    print(f"\nSources: {', '.join(sources)}")
    print(f"Scanned {scan_stats['scanned']} files ({scan_stats['ignored']} skipped via dedup ignore list, "
          f"{scan_stats['hashed']} hashed)")
    print("Changes: " + ", ".join(f"{counts[k]} {k}" for k in counts))
    if not can_embed:
        print("Mode: manifest-only (sentence-transformers/chromadb unavailable)")
    if not args.dry_run:
        print(f"Indexed: {stats.get('chunks_indexed', 0)} chunks"
              + (f", re-labelled {stats['chunks_moved']}" if stats.get("chunks_moved") else ""))
        if stats.get("chunks_pending"):
            print(f"Chunked but not indexed: {stats['chunks_pending']} chunks")
        not_indexed: dict[str, list[str]] = defaultdict(list)
        for rel, entry in sorted(current.items()):
            status = entry.get("status")
            if status and status != INDEXED:
                not_indexed[status].append(rel)
        if not_indexed:
            print("Not indexed: " + ", ".join(f"{len(paths)} {status}"
                                              for status, paths in not_indexed.items()))
            for status, paths in not_indexed.items():
                for rel in paths[:5]:
                    note = current[rel].get("note", "")
                    print(f"  - {rel} ({status}{': ' + note if note else ''})")
                if len(paths) > 5:
                    print(f"  - ... and {len(paths) - 5} more {status}")
        print(f"Manifest: {manifest_path}")
    else:
        print("Dry run: nothing written")
        for change in changes:
            if change["type"] == UNCHANGED:
                continue
            old = f"  (was {change['old_path']})" if change.get("old_path") else ""
            reason = f"  ({change['reason']})" if change.get("reason") else ""
            print(f"  would {change['type']:<9} {change['path']}{old}{reason}")

    if args.strict:
        # Unsupported formats are a config choice (xlsx/csv are still listed in
        # config.yaml), so only genuinely blocked files fail a cron run.
        blocked = {status: sum(1 for e in current.values() if e.get("status") == status)
                   for status in (NEEDS_OCR, PENDING_EMBEDDING, ERROR)}
        blocked = {status: count for status, count in blocked.items() if count}
        if blocked:
            summary = ", ".join(f"{count} {status}" for status, count in blocked.items())
            print(f"Strict mode: {summary} (rerun once the missing capability is available)",
                  file=sys.stderr)
            return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
