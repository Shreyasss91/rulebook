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
import base64
import csv
import fnmatch
import hashlib
import io
import json
import os
import re
import sys
import urllib.request
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

try:
    import openpyxl
except ImportError:
    openpyxl = None

try:
    import xlrd  # legacy .xls only; xlrd 2.x dropped xlsx support
except ImportError:
    xlrd = None

# OCR engines are optional and need native binaries/credentials of their own; a
# missing one is a capability gap (files stay needs_ocr), never a crash.
try:
    import pytesseract  # needs the Tesseract binary + language data installed
except ImportError:
    pytesseract = None

try:
    import ocrmypdf  # needs Ghostscript; only used for the searchable-PDF artefact
except ImportError:
    ocrmypdf = None


SCHEMA_VERSION = 2  # v2 adds the OCR fields (ocr_fingerprint, ocr_pages, ocr_pdf_*)
HASH_CHARS = 16
DEFAULT_MAX_RETRIES = 3

# Office owner/lock files (~$Doc.docx from Word/Excel, .~lock.Doc.odt# from
# LibreOffice/OpenOffice) are transient artefacts created while a document is open:
# they hold no real content and disappear with the application. They are skipped at
# scan time instead of being retried as `error` (see lock_file_patterns in config.yaml).
DEFAULT_LOCK_FILE_PATTERNS = ("~$*", ".~lock.*#")

# OCR backends. `hybrid` runs Tesseract first and escalates pages it could not read
# to Novita; the others pin a single engine. See docs/ocr_pass_design.md.
OCR_OFF = "none"
OCR_TESSERACT = "tesseract"
OCR_NOVITA = "novita"
OCR_HYBRID = "hybrid"
DEFAULT_OCR_BACKEND = OCR_HYBRID
DEFAULT_OCR_LANGUAGES = "eng+kan"
DEFAULT_OCR_DPI = 300
DEFAULT_OCR_ESCALATE_BELOW_CHARS = 200
DEFAULT_OCR_MAX_PAGES_PER_FILE = 500
DEFAULT_OCR_API_URL = "https://api.novita.ai/v3/openai/chat/completions"
DEFAULT_OCR_API_MODEL = "deepseek/deepseek-ocr-2"
DEFAULT_OCR_API_KEY_ENV = "NOVITA_API_KEY"
DEFAULT_OCR_API_COST_PER_PAGE = 0.0001     # Novita DeepSeek OCR 2, upper estimate
OCR_API_PROMPT = (
    "Transcribe every character on this scanned document page verbatim, in "
    "{languages} where present. Output plain text only: no commentary, no markdown "
    "fences. Preserve line breaks and rule/section numbering."
)
# A page counts as having no text layer below this many characters.
BLANK_PAGE_CHARS = 10

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

# Fallback used only when no explicit OCR context is supplied (direct calls and
# tests). Real runs compute availability from config + installed engines in main()
# and pass it through classify(), so this default never enables OCR by itself.
OCR_AVAILABLE = False

# Manifest entry fields that survive from the previous run.
CARRIED_FIELDS = ("content_hash", "signature", "page_count", "has_text_layer",
                  "pages_without_text", "chunk_count", "status", "last_processed",
                  "attempts", "note",
                  # OCR results: the settings the file was assessed under and what
                  # came out of it, so a move does not look like it was never OCR'd.
                  "ocr_fingerprint", "ocr_pages", "ocr_pdf_hash", "ocr_pdf_fingerprint")

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
    version = data.get("schema_version") if isinstance(data, dict) else None
    if isinstance(version, int) and version > SCHEMA_VERSION:
        # Refuse rather than silently re-processing a manifest written by a newer
        # script: the risk is dropping entries the newer schema knows about.
        raise ValueError(
            f"manifest {path} uses schema_version {version}, but this script understands "
            f"{SCHEMA_VERSION} - upgrade the script instead of downgrading the manifest")
    files = data.get("files", data)
    return files if isinstance(files, dict) else {}


def normalize_path(value: str | Path) -> str:
    """Case/separator-insensitive key, so D:/x and d:\\x compare equal on Windows."""
    return os.path.normcase(str(Path(value).expanduser()))


def save_manifest(path: Path, files: dict, stats: dict) -> None:
    payload = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "stats": stats,
        "files": dict(sorted(files.items())),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    # ensure_ascii=False keeps Kannada/Unicode filenames readable in the manifest.
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)  # atomic-ish: never leave a half-written manifest behind


def load_ignore_list(config: dict) -> set[str]:
    """
    Paths the dedup run decided to skip.

    The dedup script writes `str(relative_to(source))`, which on Windows contains
    backslashes, while the manifest keys are POSIX-style. Separators are normalised
    here and absolute entries are indexed by their normalised form too, so the
    ignore list keeps working whatever separator the file was produced with.
    """
    path = Path(config.get("ignore_list_path", ""))
    if not path.exists():
        return set()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return set()
    if not isinstance(data, list):
        return set()
    entries = set()
    for item in data:
        text = str(item).replace("\\", "/")
        # Absolute entries only get their normalised form, so a matched entry does
        # not leave a twin behind that looks stale.
        entries.add(normalize_path(text) if Path(text).is_absolute() else text)
    return entries


# --------------------------------------------------------------------------
# Scan
# --------------------------------------------------------------------------

def is_lock_file(name: str, patterns: list[str] | tuple[str, ...]) -> bool:
    """True when a file *name* matches an Office owner/lock pattern (not a path)."""
    return any(fnmatch.fnmatch(name, pattern) for pattern in patterns)


def hash_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(chunk_size), b""):
            digest.update(block)
    return digest.hexdigest()[:HASH_CHARS]


def scan_sources(config: dict, previous: dict, full_hash: bool) -> tuple[dict, dict]:
    """
    Build the current manifest.

    Files whose size and mtime match the previous manifest are carried over
    without re-reading them; anything else gets a fresh content hash. Entries in
    the deduplication ignore list are matched on their path relative to their
    source root - the same convention `create_deduplication_ignore_list_v2.py`
    writes - and are left out of the manifest entirely. Office owner/lock files
    (`lock_file_patterns`, e.g. `~$VER PAGE.docx`) are dropped before the
    extension filter, so they never enter the manifest as `error` entries.

    Two situations are handled defensively because they look like mass deletions:

    * an unreachable source (unmounted drive, renamed folder) - its previous
      entries are kept as-is instead of being reported as DELETED, so a missing
      D: drive cannot wipe the collection;
    * overlapping/nested sources - a file reachable through two roots is only
      processed once, under the first root that sees it.
    """
    sources = config["docs_source"]
    file_types = config.get("file_types", ["pdf"])
    batch_size = config.get("batch_size", 100)
    ignore = load_ignore_list(config)
    extensions = file_types_extensions(file_types)
    lock_patterns = config.get("lock_file_patterns", DEFAULT_LOCK_FILE_PATTERNS)

    current: dict[str, dict] = {}
    counters = {"scanned": 0, "ignored": 0, "hashed": 0, "unreadable": 0,
                "overlap": 0, "kept_offline": 0, "lock_files": 0}
    matched_ignores: set[str] = set()
    seen_paths: set[str] = set()
    unreachable: list[str] = []

    for source in sources:
        root = Path(source).expanduser()
        if not root.exists():
            unreachable.append(str(root))
            continue
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            # Checked before the extension filter so an owner/lock file is dropped
            # for what it is, not because its extension is not in file_types.
            if is_lock_file(path.name, lock_patterns):
                counters["lock_files"] += 1
                continue
            if path.suffix.lower() not in extensions:
                continue

            relative = path.relative_to(root).as_posix()
            absolute = normalize_path(path)
            if absolute in seen_paths:
                counters["overlap"] += 1
                continue
            seen_paths.add(absolute)
            counters["scanned"] += 1

            matched_ignore = relative if relative in ignore else (
                absolute if absolute in ignore else None)
            if matched_ignore is not None:
                counters["ignored"] += 1
                matched_ignores.add(matched_ignore)
                continue

            try:
                stat = path.stat()
            except OSError as exc:
                counters["unreadable"] += 1
                current[relative] = {
                    "root": str(root), "size": None, "mtime": None,
                    "status": ERROR, "note": f"unreadable: {type(exc).__name__}: {exc}",
                }
                continue

            prev = previous.get(relative)
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
                current[relative] = entry
                continue

            try:
                entry["content_hash"] = hash_file(path)
                if get_file_signature is not None:
                    entry["signature"] = get_file_signature(path, config)
            except OSError as exc:
                counters["unreadable"] += 1
                entry.update({"status": ERROR, "attempts": 0,
                              "note": f"unreadable: {type(exc).__name__}: {exc}"})
                current[relative] = entry
                continue

            counters["hashed"] += 1
            current[relative] = entry

            if counters["scanned"] % batch_size == 0:
                print(f"  scanned {counters['scanned']} files...", flush=True)

    # A source that is simply not there (drive unplugged) must not look like every
    # file in it was deleted: keep those entries untouched this run.
    unreachable_keys = {normalize_path(root) for root in unreachable}
    for relative, entry in previous.items():
        if relative not in current and normalize_path(entry.get("root", "")) in unreachable_keys:
            current[relative] = dict(entry)
            counters["kept_offline"] += 1

    # Content duplicates that the dedup ignore list does not cover would be indexed
    # twice under two ids; report them rather than silently doubling the corpus.
    by_hash: dict[str, list[str]] = defaultdict(list)
    for relative, entry in current.items():
        if entry.get("content_hash"):
            by_hash[entry["content_hash"]].append(relative)
    counters["duplicate_groups"] = sorted(
        sorted(paths) for paths in by_hash.values() if len(paths) > 1)

    counters["stale_ignores"] = sorted(ignore - matched_ignores)
    counters["unreachable"] = unreachable
    counters["nested_sources"] = nested_source_pairs(sources)
    return current, counters


def file_types_extensions(file_types: list[str]) -> set[str]:
    return {f".{ext.lower().lstrip('.')}" for ext in file_types}


def nested_source_pairs(sources: list[str]) -> list[tuple[str, str]]:
    """(outer, inner) source pairs where one configured root sits inside another."""
    roots = [Path(source).expanduser() for source in sources]
    pairs = []
    for outer in roots:
        for inner in roots:
            if outer != inner and inner.is_relative_to(outer):
                pairs.append((str(outer), str(inner)))
    return pairs


# --------------------------------------------------------------------------
# OCR pass
# --------------------------------------------------------------------------

def combine_notes(*notes: str | None) -> str | None:
    """Join non-empty notes for the manifest, or None so stale notes are cleared."""
    return "; ".join(note for note in notes if note) or None


class OcrEngine:
    """One OCR backend. `available()` is cheap and is called once at start-up."""

    name = "none"

    def __init__(self, config: dict) -> None:
        self.config = config

    def available(self) -> bool:
        return False

    def ocr_image(self, image) -> str:
        raise NotImplementedError


class TesseractEngine(OcrEngine):
    """Local Tesseract via pytesseract - free, needs the binary and language data."""

    name = OCR_TESSERACT

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        self.languages = str(config.get("ocr_languages", DEFAULT_OCR_LANGUAGES))
        self._available: bool | None = None

    def wanted_languages(self) -> set[str]:
        return {lang for lang in self.languages.split("+") if lang}

    def _installed_languages(self) -> set[str]:
        if pytesseract is None:
            return set()
        try:
            # Requires the Tesseract binary; raises (TesseractNotFoundError or an
            # OSError) when it is not installed, which is exactly the check we want.
            return set(pytesseract.get_languages(config=""))
        except Exception:
            return set()

    def missing_languages(self) -> list[str]:
        return sorted(self.wanted_languages() - self._installed_languages())

    def available(self) -> bool:
        if self._available is not None:
            return self._available
        installed = self._installed_languages()
        # Languages are strict: a missing pack must not silently fall back to
        # English, which would garble the Kannada half of a mixed order.
        self._available = bool(installed) and self.wanted_languages().issubset(installed)
        return self._available

    def ocr_image(self, image) -> str:
        return pytesseract.image_to_string(image, lang=self.languages) or ""


class NovitaEngine(OcrEngine):
    """
    Novita.ai DeepSeek OCR 2 over its OpenAI-compatible endpoint.

    Only reached for pages Tesseract could not read (or when it is the only engine),
    so paid calls stay on the hard pages. URL, model, key variable and timeout come
    from config, so a model or endpoint change is a config edit, not a code change.
    """

    name = OCR_NOVITA

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        self.url = config.get("ocr_api_url", DEFAULT_OCR_API_URL)
        self.model = config.get("ocr_api_model", DEFAULT_OCR_API_MODEL)
        self.key_env = config.get("ocr_api_key_env", DEFAULT_OCR_API_KEY_ENV)
        self.timeout = config.get("ocr_api_timeout", 60)
        self.languages = str(config.get("ocr_languages", DEFAULT_OCR_LANGUAGES))

    def api_key(self) -> str:
        return os.environ.get(self.key_env, "").strip()

    def available(self) -> bool:
        return bool(self.api_key())

    def payload(self, image) -> dict:
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
        return {
            "model": self.model,
            "temperature": 0,
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "text",
                     "text": OCR_API_PROMPT.format(languages=self.languages)},
                    {"type": "image_url",
                     "image_url": {"url": f"data:image/png;base64,{encoded}"}},
                ],
            }],
        }

    def ocr_image(self, image) -> str:
        request = urllib.request.Request(
            self.url,
            data=json.dumps(self.payload(image)).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {self.api_key()}"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            body = json.loads(response.read().decode("utf-8"))
        return extract_api_text(body)


def extract_api_text(body: dict) -> str:
    """Pull the assistant text out of an OpenAI-compatible chat response."""
    if not isinstance(body, dict):
        return ""
    try:
        content = body["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        # Some gateways return content as a list of parts.
        return "".join(part.get("text", "") for part in content
                       if isinstance(part, dict))
    return ""


def build_ocr_engines(config: dict) -> list[OcrEngine]:
    """Engines for the configured backend, in priority order (primary, escalation)."""
    backend = str(config.get("ocr_backend", DEFAULT_OCR_BACKEND)).strip().lower()
    if backend in ("", "off", "false", "no", "disabled", OCR_OFF):
        return []
    tesseract = TesseractEngine(config)
    novita = NovitaEngine(config)
    if backend == OCR_TESSERACT:
        return [tesseract]
    if backend == OCR_NOVITA:
        return [novita]
    # hybrid: Tesseract first, Novita only for pages it could not read.
    return [tesseract, novita]


def ocr_available(config: dict) -> bool:
    """True when at least one configured engine can actually run right now."""
    return any(engine.available() for engine in build_ocr_engines(config))


def ocr_fingerprint(config: dict, engine_names: list[str] | None = None) -> str:
    """
    Identity of the OCR settings a file was processed under.

    Stored in the manifest so a file is re-processed when the settings - or the set
    of usable engines - change, but not on every run. OCR text is derived, so the
    file's own content_hash cannot carry this information (see docs/ocr_pass_design.md,
    section 6).
    """
    parts = [
        str(config.get("ocr_backend", DEFAULT_OCR_BACKEND)),
        str(config.get("ocr_dpi", DEFAULT_OCR_DPI)),
        str(config.get("ocr_languages", DEFAULT_OCR_LANGUAGES)),
        str(config.get("ocr_min_chars_per_page", 50)),
        str(config.get("ocr_escalate_below_chars", DEFAULT_OCR_ESCALATE_BELOW_CHARS)),
        # The endpoint and model decide what an escalated page's text looks like, so a
        # change to either must invalidate cached/assessed text as well.
        str(config.get("ocr_api_url", DEFAULT_OCR_API_URL)),
        str(config.get("ocr_api_model", DEFAULT_OCR_API_MODEL)),
        "+".join(sorted(engine_names or [])),
    ]
    return hashlib.sha1("|".join(parts).encode()).hexdigest()[:12]


def render_pdf_page(page, config: dict) -> tuple[object | None, str | None]:
    """
    Rasterise one pdfplumber page for OCR. Returns (image, error).

    pdfplumber's own renderer is used so the only new system dependency is the OCR
    engine itself (no poppler / `pdf2image`).
    """
    dpi = config.get("ocr_dpi", DEFAULT_OCR_DPI)
    try:
        return page.to_image(resolution=dpi).original, None
    except Exception as exc:  # missing Pillow/pypdfium2, damaged page, ...
        return None, f"render failed: {type(exc).__name__}: {exc}"


class OcrCache:
    """
    Per-source OCR text, keyed by content hash and OCR settings.

    One JSON object per source file. A store whose content_hash or fingerprint no
    longer matches is discarded rather than partially reused, so neither an edited
    document nor changed settings can resurrect stale text. Saved after every file,
    so an interrupted run keeps whatever it already paid for.
    """

    def __init__(self, root: str | Path | None, identifier: str, content_hash: str,
                 fingerprint: str) -> None:
        self.root = Path(root) if root else None
        self.identifier = identifier
        self.content_hash = content_hash
        self.fingerprint = fingerprint
        self.pages: dict[str, str] = {}
        self.hits = 0
        self.dirty = False
        self._load()

    @property
    def path(self) -> Path | None:
        if self.root is None:
            return None
        return self.root / f"{hashlib.sha1(self.identifier.encode()).hexdigest()[:20]}.json"

    def _load(self) -> None:
        path = self.path
        if path is None or not path.exists():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if (data.get("content_hash") == self.content_hash
                and data.get("fingerprint") == self.fingerprint
                and isinstance(data.get("pages"), dict)):
            self.pages = {str(key): str(value) for key, value in data["pages"].items()}

    def get(self, page: int) -> str | None:
        value = self.pages.get(str(page))
        if value is not None:
            self.hits += 1
        return value

    def set(self, page: int, text: str) -> None:
        self.pages[str(page)] = text
        self.dirty = True

    def save(self) -> None:
        path = self.path
        if path is None or not self.dirty:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            payload = {"content_hash": self.content_hash, "fingerprint": self.fingerprint,
                       "pages": self.pages}
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            tmp.replace(path)
            self.dirty = False
        except OSError:
            # A cache that cannot be written only costs time on the next run.
            pass


class OcrRunner:
    """
    Fill pages that have no usable text layer, one page at a time.

    Only a writing run builds one, so `--dry-run`/`--audit` can never spend money or
    touch the cache. Engines are ordered primary-then-escalation; the run-wide
    budget (`--ocr-limit`) and the per-file cap bound one invocation, and anything
    left over is picked up by the next run (the cache makes the overlap cheap).
    """

    def __init__(self, config: dict, engines: list[OcrEngine],
                 total_limit: int | None = None) -> None:
        self.config = config
        self.engines = engines
        self.fingerprint = ocr_fingerprint(config, [engine.name for engine in engines])
        self.min_chars = config.get("ocr_min_chars_per_page", 50)
        self.escalate_below = config.get("ocr_escalate_below_chars",
                                         DEFAULT_OCR_ESCALATE_BELOW_CHARS)
        self.max_pages_per_file = config.get("ocr_max_pages_per_file",
                                             DEFAULT_OCR_MAX_PAGES_PER_FILE)
        self.cache_root = config.get("ocr_cache_path")
        self.total_limit = total_limit
        self.pages_ocred = 0       # pages whose text this pass actually recovered
        self.pages_attempted = 0   # pages sent to an engine (drives --ocr-limit)
        self.ocr_failures = 0      # pages every engine failed to read
        self.cache_hits = 0
        self.engine_calls = 0
        self.escalations = 0
        self.api_pages = 0
        self.render_errors = 0
        self.deferred = 0          # pages left for a later run (budget/cap)
        self.pdfs_written = 0
        self._caches: dict[str, OcrCache] = {}

    @property
    def primary(self) -> OcrEngine | None:
        return self.engines[0] if self.engines else None

    @property
    def escalation(self) -> OcrEngine | None:
        return self.engines[1] if len(self.engines) > 1 else None

    def available(self) -> bool:
        return bool(self.engines)

    def needs_ocr(self, text: str) -> bool:
        return len(text.strip()) < self.min_chars

    def budget_left(self) -> int:
        """Pages this run may still send to an engine.

        Counts *attempted* pages, not recovered ones: a page that costs a paid API
        call but comes back empty still spent money, so it has to draw down the
        budget or `--ocr-limit` would not bound the spend.
        """
        if self.total_limit is None:
            return self.max_pages_per_file
        return max(0, self.total_limit - self.pages_attempted)

    def cache_for(self, identifier: str, content_hash: str) -> OcrCache:
        cache = self._caches.get(identifier)
        if cache is None:
            cache = OcrCache(self.cache_root, identifier, content_hash, self.fingerprint)
            self._caches[identifier] = cache
        return cache

    def flush(self) -> None:
        for cache in self._caches.values():
            cache.save()

    def fill(self, rel: str, path: Path, pages: list[dict], entry: dict) -> dict:
        """
        OCR the pages of `pages` that have no usable text, in place.

        Returns a small result dict:

        * `attempted` - candidates existed, so the manifest may record the fingerprint
          once the pass truly finished;
        * `incomplete` - the budget/cap left pages behind, so the file must stay
          un-settled and be revisited next run;
        * `errors` / `fatal` - pages every engine failed to read, and a failure that
          stopped the whole file (unopenable PDF). Failures are never settled and
          never cached as "empty";
        * `ocr_pages` - pages an engine recovered this pass (drives the report);
        * `recovered` - candidate pages that now have text, including ones answered
          from the cache, which is what qualifies the file for a searchable PDF.
        """
        result = {"pages": pages, "note": None, "ocr_pages": 0,
                  "recovered": 0, "errors": 0, "fatal": None,
                  "attempted": False, "incomplete": False}
        candidates = [page for page in pages
                      if page.get("page") is not None and self.needs_ocr(page["text"])]
        if not candidates or self.primary is None:
            return result
        result["attempted"] = True

        notes: list[str] = []
        if len(candidates) > self.max_pages_per_file:
            dropped = len(candidates) - self.max_pages_per_file
            candidates = candidates[:self.max_pages_per_file]
            result["incomplete"] = True
            self.deferred += dropped
            notes.append(f"capped at {self.max_pages_per_file} page(s) per run; "
                         f"{dropped} follow next run")

        identifier = normalize_path(Path(entry.get("root", "")) / rel)
        cache = self.cache_for(identifier, entry.get("content_hash") or "")
        by_page = {page["page"]: page for page in candidates}

        # Cache hits cost nothing, so decide what to run before opening the PDF - a
        # fully cached file never renders a page.
        allowed = self.budget_left()
        misses: list[int] = []
        for number, page in sorted(by_page.items()):
            cached = cache.get(number)
            if cached is not None:
                page["text"] = cached          # recovered by an earlier run
                self.cache_hits += 1
            elif len(misses) < allowed:
                misses.append(number)
            else:
                result["incomplete"] = True
                self.deferred += 1

        if misses:
            note = self._ocr_pages(path, misses, by_page, cache, result)
            if note:
                notes.append(note)
        cache.save()
        # Count cache-answered pages too: the file has OCR text now, so it deserves
        # the same searchable-PDF artefact whether this run or an earlier one read it.
        result["recovered"] = sum(1 for page in candidates if page["text"].strip())
        result["note"] = combine_notes(*notes)
        return result

    def _ocr_pages(self, path: Path, numbers: list[int], by_page: dict[int, dict],
                   cache: OcrCache, result: dict) -> str | None:
        rendered, open_note = self._render_pages(path, numbers)
        if open_note:
            # Nothing can be OCR'd without the pages, so this is fatal for the file.
            self.render_errors += len(numbers)
            result["fatal"] = open_note
            return open_note
        notes: list[str] = []
        empty = 0
        unrendered = 0
        failed = 0
        for number in numbers:
            image = rendered.get(number)
            if image is None:
                unrendered += 1
                continue
            self.pages_attempted += 1
            text, note, page_failed = self._read_page(image, number)
            if note:
                notes.append(note)
            if page_failed:
                # A failed page is not an empty page. Caching "" here would tell the
                # next run the page was tried and had nothing to give, so a transient
                # engine failure (timeout, restart) would become permanent. Leave it
                # uncached and report it, so the next run retries it.
                failed += 1
                continue
            # The empty string *is* cached on purpose when an engine ran successfully
            # and read nothing: it records "OCR ran on this page and found nothing",
            # so a hopeless page is not charged for again.
            cache.set(number, text or "")
            if (text or "").strip():
                by_page[number]["text"] = text
                result["ocr_pages"] += 1
                self.pages_ocred += 1
            else:
                empty += 1
        self.render_errors += unrendered
        result["errors"] += failed
        self.ocr_failures += failed
        if empty:
            notes.append(f"{empty} page(s) still without text after OCR")
        if failed:
            notes.append(f"{failed} page(s) failed OCR")
        if unrendered:
            notes.append(f"{unrendered} page(s) could not be rendered")
        return combine_notes(*notes)

    def _render_pages(self, path: Path, numbers: list[int]) -> tuple[dict[int, object], str | None]:
        """
        Rasterise the requested pages. Returns (images by page number, fatal note).

        A page that will not render is simply left out; the caller reports the count.
        Only a failure to open the PDF at all is fatal to the whole file.
        """
        if pdfplumber is None:
            return {}, "pdfplumber not installed: cannot render pages for OCR"
        rendered: dict[int, object] = {}
        try:
            with pdfplumber.open(path) as pdf:
                total = len(pdf.pages)
                for number in numbers:
                    if not 1 <= number <= total:
                        continue
                    image, _error = render_pdf_page(pdf.pages[number - 1], self.config)
                    if image is not None:
                        rendered[number] = image
        except Exception as exc:  # encrypted, truncated, unreadable
            return rendered, f"cannot open for OCR: {type(exc).__name__}: {exc}"
        return rendered, None

    def _read_page(self, image, number: int) -> tuple[str, str | None, bool]:
        """
        Read one page, escalating when the primary engine failed or read too little.

        Returns (text, note, failed). `failed` is True only when no engine produced
        text because they errored; a page that was read successfully and came back
        empty is a genuine empty page, not a failure, and an escalation that succeeds
        (even with empty text) salvages a primary failure.
        """
        primary = self.primary
        if primary is None:
            return "", None, False
        text = ""
        failed_primary = False
        notes: list[str] = []
        try:
            self.engine_calls += 1
            text = primary.ocr_image(image) or ""
        except Exception as exc:
            # An engine failure is not a render failure, so it is reported but not
            # counted against the renderer.
            failed_primary = True
            notes.append(f"page {number}: {primary.name} failed: {type(exc).__name__}: {exc}")

        escalation = self.escalation
        escalation_succeeded = False
        if escalation is not None and escalation.available() and (
                failed_primary or len(text.strip()) < self.escalate_below):
            try:
                self.engine_calls += 1
                self.api_pages += 1
                self.escalations += 1
                escalated = escalation.ocr_image(image) or ""
                escalation_succeeded = True
            except Exception as exc:
                notes.append(f"page {number}: {escalation.name} escalation failed: "
                             f"{type(exc).__name__}: {exc}")
            else:
                if len(escalated.strip()) > len(text.strip()):
                    text = escalated
        # A primary failure that the escalation could not rescue is the only true
        # page failure.
        failed = failed_primary and not escalation_succeeded
        return text, combine_notes(*notes), failed


def searchable_pdf_path(rel: str, config: dict) -> Path | None:
    root = config.get("ocr_pdf_path")
    return Path(root) / rel if root else None


def ensure_searchable_pdf(rel: str, source: Path, entry: dict, config: dict,
                          fingerprint: str) -> tuple[bool, str | None]:
    """
    Mirror one OCR'd PDF into `ocr_pdf_path` with a searchable text layer.

    `ocrmypdf --skip-text` means only pages without a layer are OCR'd - the pages
    this pass already targeted - and the corpus itself is never modified. The
    artefact is best-effort: a missing ocrmypdf/Ghostscript becomes a note rather
    than a failure, because the cached text already feeds retrieval.
    """
    if not config.get("ocr_write_searchable_pdfs", True):
        return False, None
    destination = searchable_pdf_path(rel, config)
    if destination is None:
        return False, None
    content_hash = entry.get("content_hash")
    if (destination.exists() and entry.get("ocr_pdf_hash") == content_hash
            and entry.get("ocr_pdf_fingerprint") == fingerprint):
        return False, None            # already current for this content + settings
    if ocrmypdf is None:
        # Reported once per run by report_ocr rather than noted against every file,
        # which would otherwise fill the manifest with identical notes.
        return False, None
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        ocrmypdf.ocr(str(source), str(destination), skip_text=True,
                     language=str(config.get("ocr_languages", DEFAULT_OCR_LANGUAGES)))
    except Exception as exc:
        return False, f"ocrmypdf failed: {type(exc).__name__}: {exc}"
    entry["ocr_pdf_hash"] = content_hash
    entry["ocr_pdf_fingerprint"] = fingerprint
    return True, None


# --------------------------------------------------------------------------
# Classification
# --------------------------------------------------------------------------

def needs_reprocessing(prev_entry: dict, can_embed: bool, max_retries: int,
                       ocr: dict | None = None, rel: str = "") -> str | None:
    """
    Return a reason to re-process a file whose content is unchanged, else None.

    `ocr` carries the run's OCR context ({"available": bool, "fingerprint": str}) and
    defaults to the module-level OCR_AVAILABLE flag, so direct callers and tests keep
    working without one.
    """
    ocr = ocr or {}
    ocr_available = bool(ocr.get("available", OCR_AVAILABLE))
    fingerprint = ocr.get("fingerprint") or ocr_fingerprint({})
    status = prev_entry.get("status")

    if status == NEEDS_OCR:
        if not ocr_available:
            return None
        if prev_entry.get("ocr_fingerprint") == fingerprint:
            return None   # already attempted with these engines/settings
        return "retry: OCR available"
    if status == ERROR and prev_entry.get("attempts", 0) >= max_retries:
        # Repeated failures (corrupt file, permission problem) are parked and
        # surfaced by --strict instead of retried on every run.
        return None
    # An already-indexed PDF with pages that had no text layer is exactly what the OCR
    # pass exists for, and its content_hash will never change on its own, so the
    # fingerprint is what tells "already recovered" from "never attempted".
    if (ocr_available and rel.lower().endswith(".pdf")
            and prev_entry.get("pages_without_text")
            and prev_entry.get("ocr_fingerprint") != fingerprint):
        return "ocr: pages with no text layer not yet recovered"
    if status in RETRYABLE and can_embed:
        return f"retry: previous run ended in {status}"
    return None


def classify(current: dict, previous: dict, config: dict, can_embed: bool,
             ocr: dict | None = None) -> list[dict]:
    track_moves = config.get("track_moves", True)
    track_deletions = config.get("track_deletions", True)
    max_retries = config.get("max_retries", DEFAULT_MAX_RETRIES)

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

        reason = needs_reprocessing(prev, can_embed, max_retries, ocr, rel)
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

    `pages` is a list of {"page": int|None, "text": str, "label": str|None}; page is
    None for formats without pages (docx/txt/md/sheets) and the optional label is
    what cites them instead - today the worksheet name for spreadsheets.
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

        if suffix in {".xlsx", ".xlsm"}:
            if openpyxl is None:
                return None, "openpyxl not installed"
            return extract_xlsx(path, config)

        if suffix == ".xls":
            if xlrd is None:
                return None, "xlrd not installed (needed for legacy .xls)"
            return extract_xls(path, config)

        if suffix == ".csv":
            return extract_csv(path, config)

    except Exception as exc:  # unreadable/encrypted/corrupt files must not kill the run
        return [], f"{type(exc).__name__}: {exc}"

    return None, f"{suffix} extraction not implemented"


def render_rows(rows, config: dict) -> tuple[str, int]:
    """
    Turn spreadsheet rows into text lines, one row per line, cells joined by ' | '.

    Returns (text, dropped_row_count). The cap keeps a runaway sheet (tens of
    thousands of tariff rows) from producing one enormous embedding batch.
    """
    limit = config.get("spreadsheet_max_rows", 5000)
    lines: list[str] = []
    dropped = 0
    for row in rows:
        cells = [str(value).strip() for value in row
                 if value is not None and str(value).strip()]
        if not cells:
            continue
        if len(lines) >= limit:
            dropped += 1
            continue
        lines.append(" | ".join(cells))
    return "\n".join(lines), dropped


def extract_xlsx(path: Path, config: dict) -> tuple[list[dict], str | None]:
    """One page per worksheet, labelled with the sheet name."""
    limit = config.get("spreadsheet_max_rows", 5000)
    pages: list[dict] = []
    notes: list[str] = []
    # data_only=True reads cached formula results; read_only streams big sheets.
    workbook = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        for sheet in workbook.worksheets:
            text, dropped = render_rows(sheet.iter_rows(values_only=True), config)
            if dropped:
                notes.append(f"{sheet.title}: first {limit} rows kept, {dropped} dropped")
            if text:
                pages.append({"page": None, "label": sheet.title, "text": text})
    finally:
        workbook.close()
    return pages, "; ".join(notes) or None


def extract_xls(path: Path, config: dict) -> tuple[list[dict], str | None]:
    """Legacy .xls via xlrd - same shape as the xlsx path."""
    limit = config.get("spreadsheet_max_rows", 5000)
    pages: list[dict] = []
    notes: list[str] = []
    workbook = xlrd.open_workbook(str(path), on_demand=True)
    try:
        for name in workbook.sheet_names():
            sheet = workbook.sheet_by_name(name)
            rows = (sheet.row_values(index) for index in range(sheet.nrows))
            text, dropped = render_rows(rows, config)
            if dropped:
                notes.append(f"{name}: first {limit} rows kept, {dropped} dropped")
            if text:
                pages.append({"page": None, "label": name, "text": text})
    finally:
        workbook.release_resources()
    return pages, "; ".join(notes) or None


def extract_csv(path: Path, config: dict) -> tuple[list[dict], str | None]:
    limit = config.get("spreadsheet_max_rows", 5000)
    with path.open("r", encoding="utf-8-sig", errors="ignore", newline="") as handle:
        text, dropped = render_rows(csv.reader(handle), config)
    note = f"first {limit} rows kept, {dropped} dropped" if dropped else None
    return ([{"page": None, "label": None, "text": text}] if text else []), note


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
            elif len(buffer) < minimum:
                # A runt buffer is almost always a heading line or the tail of the
                # previous chunk. Emitting it alone would index a fragment with no
                # content, so stay attached to what follows even if that overshoots
                # `chunk_size` (new content is still capped at `size` per piece).
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
        for chunk in chunk_text(page["text"], page.get("label"), config):
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
                            collection, can_embed: bool,
                            embed_note: str | None = None,
                            ocr: OcrRunner | None = None) -> tuple[str, int, str | None]:
    """
    Extract -> OCR pages with no text -> chunk -> embed -> upsert one NEW/MODIFIED file.

    OCR sits between extraction and chunking, on the same page dicts, so recovered
    text keeps its page number and flows through the unchanged chunking path.
    """
    rel = change["path"]
    entry = change["entry"]
    path = Path(entry["root"]) / rel
    suffix = path.suffix.lower()

    if not entry.get("content_hash"):
        # Unreadable at scan time: chunk ids could not be derived, so leave it alone.
        return ERROR, 0, entry.get("note") or "no content hash (file unreadable)"

    if entry.get("size") == 0:
        return EMPTY, 0, "zero-byte file"

    pages, error = extract_pages(path, config)
    if pages is None:
        return UNSUPPORTED, 0, error
    if error:
        return ERROR, 0, error

    ocr_result = None
    if ocr is not None and ocr.available() and suffix == ".pdf":
        ocr_result = ocr.fill(rel, path, pages, entry)
        settled = (ocr_result["attempted"] and not ocr_result["incomplete"]
                   and not ocr_result["errors"] and not ocr_result["fatal"])
        if settled:
            # OCR text is derived, so the content hash cannot tell a recovered file
            # from one that was never attempted - the fingerprint is what settles it.
            # A failed or capped pass stays un-settled and is retried next run.
            entry["ocr_fingerprint"] = ocr.fingerprint
        if ocr_result["ocr_pages"]:
            entry["ocr_pages"] = ocr_result["ocr_pages"]
    ocr_note = ocr_result["note"] if ocr_result else None
    # A missing tool must park a file politely; a broken engine or an unopenable PDF
    # must instead be recorded as an error that is retried and eventually parked.
    ocr_error = None
    if ocr_result and ocr_result["fatal"]:
        ocr_error = ocr_result["fatal"]
    elif ocr_result and ocr_result["errors"] and not ocr_result["recovered"]:
        ocr_error = ocr_note or "OCR failed on every candidate page"

    blank_pages = [page for page in pages if len(page["text"].strip()) < BLANK_PAGE_CHARS]
    has_text = sum(len(page["text"].strip()) for page in pages) > 0

    if not has_text:
        # An image-only PDF needs OCR; a blank text file is just empty.
        if suffix == ".pdf":
            if ocr_error:
                return ERROR, 0, ocr_error
            if ocr is not None and ocr.available():
                return NEEDS_OCR, 0, combine_notes(
                    "no text layer (image-only PDF) - OCR recovered no text", ocr_note)
            return NEEDS_OCR, 0, "no text layer (image-only PDF) - OCR not available"
        return EMPTY, 0, "no extractable text"

    if suffix == ".pdf" and not has_text_layer(pages, config):
        minimum = config.get("ocr_min_chars_per_page", 50)
        if ocr_error:
            return ERROR, 0, ocr_error
        if ocr_result and ocr_result["ocr_pages"]:
            # Partial recovery: index what came back instead of dropping the whole
            # document, and let pages_without_text record the remainder.
            ocr_note = combine_notes(ocr_note, "low text layer: only partly recovered")
        elif ocr is not None and ocr.available():
            return NEEDS_OCR, 0, combine_notes(
                f"text layer below {minimum} chars/page - OCR recovered no text", ocr_note)
        else:
            return NEEDS_OCR, 0, (f"text layer below {minimum} chars/page - "
                                  "OCR not available")

    chunks = build_chunks(pages, config)

    if not chunks:
        return EMPTY, 0, "extracted text produced no chunks"

    if ocr is not None and ocr_result and ocr_result["recovered"]:
        # Only files that actually have OCR text get an artefact - whether this run or
        # an earlier one read it - so the mirror stays a fraction of the corpus rather
        # than a full second copy. The entry's hash+fingerprint guard makes it a no-op
        # when the artefact is already current.
        written, pdf_note = ensure_searchable_pdf(rel, path, entry, config, ocr.fingerprint)
        if written:
            ocr.pdfs_written += 1
        ocr_note = combine_notes(ocr_note, pdf_note)

    entry["page_count"] = len(pages)
    entry["has_text_layer"] = True
    entry["pages_without_text"] = len(blank_pages)
    entry["chunk_count"] = len(chunks)

    if not can_embed or collection is None:
        # Chunks are already built, so the next run with the deps installed only
        # has to embed them - the chunk_count is recorded as computed.
        return PENDING_EMBEDDING, len(chunks), combine_notes(
            embed_note or "sentence-transformers/chromadb not installed", ocr_note)

    indexed = index_chunks(collection, embedder, rel, entry["root"], path, chunks,
                           entry["content_hash"])
    return INDEXED, indexed, ocr_note


def apply_one_change(change: dict, config: dict, previous: dict, current: dict, stats: dict,
                     embedder: Embedder, collection, can_embed: bool, verbose: bool,
                     embed_note: str | None = None, ocr: OcrRunner | None = None) -> None:
    """Apply a single classified change, updating `current` and `stats` in place."""
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
        return

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    if kind == DELETED:
        if collection is not None:
            delete_chunks(collection, rel)
        current.pop(rel, None)
        log(f"  {DELETED:<9} {rel}")
        return

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
        return

    status, chunk_count, note = process_new_or_modified(
        {"path": rel, "entry": entry}, config, embedder, collection, can_embed, embed_note, ocr)
    entry["status"] = status
    entry["last_processed"] = now
    entry["chunk_count"] = chunk_count
    # Attempts drive the retry ceiling for repeated failures; settled files reset it.
    if status in RETRYABLE:
        entry["attempts"] = entry.get("attempts", 0) + 1
    else:
        entry["attempts"] = 0
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


def apply_changes(changes: list[dict], config: dict, previous: dict, current: dict,
                  can_embed: bool, verbose: bool, write: bool, extract: bool,
                  save_cb=None, embed_note: str | None = None,
                  ocr: OcrRunner | None = None) -> dict:
    """
    Apply every change.

    `write`    - False keeps the manifest and the vector store untouched.
    `extract`  - False only classifies (the cheap `--dry-run`); True runs extraction
                 and chunking, which is what `--audit` uses to measure the text
                 layer without embedding anything.
    `save_cb`  - called every `batch_size` changes, so an interrupted run on the
                 full corpus keeps its progress.
    """
    stats = defaultdict(int)
    embedder = Embedder(config)
    collection = None if not write else open_collection(config)
    save_every = config.get("batch_size", 100)
    pending = sum(1 for change in changes if change["type"] != UNCHANGED)
    processed = 0

    for change in changes:
        if not extract and change["type"] in (NEW, MODIFIED, DELETED):
            # Nothing to apply; planned actions are listed in the summary below.
            stats[change["type"]] += 1
            continue

        # UNCHANGED and MOVED always go through: the log line and the "moved but
        # never indexed" reclassification belong in the report.
        apply_one_change(change, config, previous, current, stats, embedder,
                         collection, can_embed, verbose, embed_note, ocr)

        if not write or change["type"] == UNCHANGED:
            continue
        processed += 1
        if save_every and processed % save_every == 0:
            if save_cb:
                save_cb()
            print(f"  ... {processed}/{pending} changes processed", flush=True)

    return stats


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def report_not_indexed(current: dict) -> None:
    """Print the files that are in the manifest but not in the collection, by status."""
    not_indexed: dict[str, list[str]] = defaultdict(list)
    for rel, entry in sorted(current.items()):
        status = entry.get("status")
        if status and status != INDEXED:
            not_indexed[status].append(rel)
    if not not_indexed:
        return
    print("Not indexed: " + ", ".join(f"{len(paths)} {status}"
                                      for status, paths in not_indexed.items()))
    for status, paths in not_indexed.items():
        for rel in paths[:5]:
            note = current[rel].get("note", "")
            print(f"  - {rel} ({status}{': ' + note if note else ''})")
        if len(paths) > 5:
            print(f"  - ... and {len(paths) - 5} more {status}")


def report_ocr(runner: OcrRunner | None, config: dict, requested: bool) -> None:
    """Print what the OCR pass did, or why a requested pass did not run."""
    if runner is None:
        if requested:
            backend = config.get("ocr_backend", DEFAULT_OCR_BACKEND)
            languages = config.get("ocr_languages", DEFAULT_OCR_LANGUAGES)
            key_env = config.get("ocr_api_key_env", DEFAULT_OCR_API_KEY_ENV)
            print(f"OCR: backend '{backend}' is configured but no engine is available "
                  f"(install Tesseract with '{languages}' and/or set {key_env}); scanned "
                  "pages stay needs_ocr", file=sys.stderr)
        return
    parts = [f"{runner.pages_ocred} page(s) OCR'd"]
    if runner.cache_hits:
        parts.append(f"{runner.cache_hits} cache hit(s)")
    parts.append(f"{runner.engine_calls} engine call(s)")
    if runner.escalations:
        parts.append(f"{runner.escalations} escalated")
    print("OCR: " + ", ".join(parts))
    if runner.ocr_failures:
        print(f"OCR: {runner.ocr_failures} page(s) failed and will be retried next run",
              file=sys.stderr)
    if runner.api_pages:
        per_page = config.get("ocr_api_cost_per_page", DEFAULT_OCR_API_COST_PER_PAGE)
        print(f"OCR API: {runner.api_pages} page(s) via {OCR_NOVITA}, "
              f"~${runner.api_pages * per_page:.4f} estimated")
    if runner.pdfs_written:
        print(f"Searchable PDFs: {runner.pdfs_written} mirrored to "
              f"{config.get('ocr_pdf_path')}")
    elif ocrmypdf is None and config.get("ocr_write_searchable_pdfs", True) \
            and (runner.pages_ocred or runner.cache_hits):
        print("Searchable PDFs: skipped (ocrmypdf not installed)")
    if runner.deferred:
        print(f"OCR deferred: {runner.deferred} page(s) left for a later run "
              "(--ocr-limit / ocr_max_pages_per_file)")
    if runner.render_errors:
        print(f"OCR: {runner.render_errors} page(s) could not be rendered", file=sys.stderr)


def report_warnings(scan_stats: dict, current: dict, changes: list[dict]) -> None:
    """Surface the situations that need a human eye, not just a status change."""
    for source in scan_stats.get("unreachable", []):
        print(f"Warning: source not reachable, kept {scan_stats['kept_offline']} file(s) "
              f"as-is instead of deleting them: {source}", file=sys.stderr)
    for outer, inner in scan_stats.get("nested_sources", []):
        print(f"Warning: source {inner} is inside {outer}; those files are processed once "
              "under the outer root", file=sys.stderr)
    if scan_stats.get("overlap"):
        print(f"Note: {scan_stats['overlap']} file(s) reachable through two sources were "
              "counted once")
    if scan_stats.get("lock_files"):
        print(f"Note: {scan_stats['lock_files']} Office owner/lock file(s) skipped "
              f"({', '.join(DEFAULT_LOCK_FILE_PATTERNS)})")
    if scan_stats.get("unreadable"):
        print(f"Note: {scan_stats['unreadable']} file(s) could not be read", file=sys.stderr)
    duplicates = scan_stats.get("duplicate_groups") or []
    if duplicates:
        extra = len(duplicates) - 3
        print(f"Note: {len(duplicates)} content duplicate group(s) are not in the dedup "
              f"ignore list, so each copy is indexed: {duplicate_sample(duplicates)}"
              + (f" (+{extra} more)" if extra > 0 else ""))
    # With --source the ignore list still belongs to the configured sources, so
    # "stale entry" would be reported for every file outside the override scope.
    if scan_stats.get("stale_ignores") and not scan_stats.get("sources_overridden"):
        print(f"Note: {len(scan_stats['stale_ignores'])} ignore-list entr(ies) match no "
              f"current file (stale after a move or rename): "
              f"{', '.join(scan_stats['stale_ignores'][:3])}")
    mixed = [(rel, entry) for rel, entry in sorted(current.items())
             if entry.get("pages_without_text")]
    if mixed:
        pages = sum(entry["pages_without_text"] for _, entry in mixed)
        print(f"Note: {len(mixed)} indexed file(s) still have {pages} page(s) with no "
              "text layer (OCR candidates; run without --no-ocr to recover them)")
    for hint in rename_hints(changes):
        print(f"Note: {hint}")


def duplicate_sample(duplicates: list[list[str]]) -> str:
    return "; ".join(" = ".join(paths) for paths in duplicates[:3])


def rename_hints(changes: list[dict]) -> list[str]:
    """
    A NEW file sharing a dedup signature with a vanished one is usually a rename
    plus an edit. Content differs, so it cannot be a move - but leaving both sets of
    chunks indexes the document twice, so flag it for a human instead.
    """
    deleted = {change["entry"].get("signature"): change["path"] for change in changes
               if change["type"] == DELETED and change["entry"].get("signature")}
    hints = []
    for change in changes:
        if change["type"] != NEW:
            continue
        signature = change["entry"].get("signature")
        if signature and signature in deleted:
            hints.append(f"{deleted[signature]} disappeared while {change['path']} appeared with "
                         "the same content signature - if it was renamed and edited, the old "
                         "chunks need removing to avoid duplicates")
    return hints


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
    parser.add_argument("--audit", action="store_true",
                        help="like --dry-run but also extracts and chunks, so needs_ocr, "
                             "empty and unsupported volumes can be measured; writes nothing")
    parser.add_argument("--full-hash", action="store_true",
                        help="re-hash every file instead of trusting size+mtime")
    parser.add_argument("--strict", action="store_true",
                        help="exit non-zero if any file is left pending (for cron)")
    parser.add_argument("--no-ocr", action="store_true",
                        help="skip the OCR pass even when an engine is available")
    parser.add_argument("--ocr-limit", type=int, default=None, metavar="PAGES",
                        help="OCR at most PAGES page(s) this run; the rest follow next run")
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

    try:
        previous = load_manifest(manifest_path)
    except (ValueError, json.JSONDecodeError) as exc:
        print(f"Cannot read manifest: {exc}", file=sys.stderr)
        return 1
    current, scan_stats = scan_sources(config, previous, args.full_hash)
    scan_stats["sources_overridden"] = bool(args.source)

    # --audit extracts text but never embeds: that is what makes it read-only.
    write = not (args.dry_run or args.audit)
    extract = write or args.audit
    can_embed = write and Embedder.available() and chromadb is not None

    # OCR only ever runs on a writing run: --dry-run and --audit must be free, so
    # they never build engines, never render a page and never touch the cache.
    ocr_backend = str(config.get("ocr_backend", DEFAULT_OCR_BACKEND)).strip().lower()
    disabled_backends = ("", "off", "false", "no", "disabled", OCR_OFF)
    if ocr_backend not in disabled_backends + (OCR_TESSERACT, OCR_NOVITA, OCR_HYBRID):
        # A typo must not silently pick a backend - especially not the one that spends.
        print(f"Warning: unknown ocr_backend '{ocr_backend}'; using "
              f"'{DEFAULT_OCR_BACKEND}'", file=sys.stderr)
        ocr_backend = DEFAULT_OCR_BACKEND
        config["ocr_backend"] = DEFAULT_OCR_BACKEND
    ocr_requested = write and not args.no_ocr and ocr_backend not in disabled_backends
    ocr_runner = None
    if ocr_requested:
        engines = [engine for engine in build_ocr_engines(config) if engine.available()]
        if engines:
            ocr_runner = OcrRunner(config, engines, total_limit=args.ocr_limit)
    ocr_ctx = {
        "available": ocr_runner is not None,
        "fingerprint": ocr_fingerprint(
            config, [engine.name for engine in (ocr_runner.engines if ocr_runner else [])]),
    }

    changes = classify(current, previous, config, can_embed, ocr_ctx)

    def save_now() -> None:
        save_manifest(manifest_path, current, {"scanned": scan_stats["scanned"],
                                              "ignored": scan_stats["ignored"]})

    stats = apply_changes(changes, config, previous, current, can_embed,
                          args.verbose, write=write, extract=extract,
                          save_cb=save_now if write else None,
                          embed_note="--audit: extraction only, not indexed" if args.audit else None,
                          ocr=ocr_runner)

    if ocr_runner is not None:
        # Persist whatever was paid for, even if the run is interrupted later.
        ocr_runner.flush()

    if write:
        # Final save; apply_changes also saved every batch_size changes.
        save_now()

    counts = {kind: sum(1 for c in changes if c["type"] == kind)
              for kind in (NEW, MODIFIED, MOVED, DELETED, UNCHANGED)}
    print(f"\nSources: {', '.join(sources)}")
    skipped = [f"{scan_stats['ignored']} skipped via dedup ignore list"]
    if scan_stats.get("lock_files"):
        skipped.append(f"{scan_stats['lock_files']} lock file(s) skipped")
    skipped.append(f"{scan_stats['hashed']} hashed")
    print(f"Scanned {scan_stats['scanned']} files ({', '.join(skipped)})")
    print("Changes: " + ", ".join(f"{counts[k]} {k}" for k in counts))
    if write and not can_embed:
        print("Mode: manifest-only (sentence-transformers/chromadb unavailable)")
    if args.audit:
        print("Mode: audit (--audit) - extracted, chunked, nothing written")
    report_warnings(scan_stats, current, changes)
    report_ocr(ocr_runner, config, requested=ocr_requested)

    if write:
        print(f"Indexed: {stats.get('chunks_indexed', 0)} chunks"
              + (f", re-labelled {stats['chunks_moved']}" if stats.get("chunks_moved") else ""))
        if stats.get("chunks_pending"):
            print(f"Chunked but not indexed: {stats['chunks_pending']} chunks")
        report_not_indexed(current)
        print(f"Manifest: {manifest_path}")
    elif extract:
        would_index = [rel for rel, entry in sorted(current.items())
                       if entry.get("status") == PENDING_EMBEDDING]
        pages = sum(entry.get("page_count") or 0 for entry in current.values())
        no_text = sum(entry.get("pages_without_text") or 0 for entry in current.values())
        print(f"Would index: {len(would_index)} file(s), {stats.get('chunks_pending', 0)} chunks "
              f"from {pages} page(s)")
        if no_text:
            print(f"Pages with no text layer: {no_text} "
                  f"({no_text / max(pages, 1):.0%} of extracted pages) - OCR candidates; "
                  "audit mode never OCRs, run the script without --audit to fill them")
        report_not_indexed(current)
        print("Audit: nothing written")
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
