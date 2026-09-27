"""
Tests for the manifest-based incremental ingestion pipeline.

The vector store and the embedding model are faked, so the suite runs without
sentence-transformers, chromadb or a model download.
"""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import pytest
import yaml

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import incremental_ingest as ingest  # noqa: E402  (import after sys.path tweak)


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

class FakeCollection:
    """Minimal stand-in for a ChromaDB collection, recording what it was asked to do."""

    def __init__(self) -> None:
        self.docs: dict[str, dict] = {}
        self.calls: list[tuple] = []

    def upsert(self, ids, documents, embeddings, metadatas) -> None:
        for chunk_id, document, metadata in zip(ids, documents, metadatas):
            self.docs[chunk_id] = {"document": document, "metadata": dict(metadata)}
        self.calls.append(("upsert", len(ids)))

    def delete(self, where=None) -> None:
        source = (where or {}).get("source")
        doomed = [i for i, entry in self.docs.items() if entry["metadata"].get("source") == source]
        for chunk_id in doomed:
            self.docs.pop(chunk_id)
        self.calls.append(("delete", source, len(doomed)))

    def get(self, where=None, include=None) -> dict:
        source = (where or {}).get("source")
        ids = [i for i, entry in self.docs.items() if entry["metadata"].get("source") == source]
        return {"ids": ids, "metadatas": [dict(self.docs[i]["metadata"]) for i in ids]}

    def update(self, ids, metadatas) -> None:
        for chunk_id, metadata in zip(ids, metadatas):
            self.docs[chunk_id]["metadata"] = dict(metadata)
        self.calls.append(("update", len(ids)))

    def count(self) -> int:
        return len(self.docs)

    def sources(self) -> set[str]:
        return {entry["metadata"].get("source") for entry in self.docs.values()}


@pytest.fixture
def store(monkeypatch) -> tuple[FakeCollection, dict]:
    """A fake collection + counter, wired into the module under test."""
    collection = FakeCollection()
    counters = {"embedded": 0}

    monkeypatch.setattr(ingest, "chromadb", types.SimpleNamespace(), raising=False)
    monkeypatch.setattr(ingest.Embedder, "available", staticmethod(lambda: True))
    monkeypatch.setattr(ingest.Embedder, "encode",
                        lambda self, texts: _fake_vectors(texts, counters))
    monkeypatch.setattr(ingest, "open_collection", lambda config: collection)
    return collection, counters


def _fake_vectors(texts: list[str], counters: dict) -> list[list[float]]:
    counters["embedded"] += len(texts)
    return [[0.1, 0.2] for _ in texts]


@pytest.fixture
def no_deps(monkeypatch) -> None:
    """Pretend sentence-transformers/chromadb are not installed."""
    monkeypatch.setattr(ingest, "chromadb", None)
    monkeypatch.setattr(ingest.Embedder, "available", staticmethod(lambda: False))


def write_config(tmp_path: Path, corpus: Path, **overrides) -> Path:
    config = {
        "docs_source": [str(corpus)],
        "file_types": ["txt", "md", "pdf"],
        "ignore_list_path": str(tmp_path / "ignore.json"),
        "progress_path": str(tmp_path / "progress.json"),
        "manifest_path": str(tmp_path / "manifest.json"),
        "batch_size": 100,
        "signature_prefix_chars": 300,
        "track_moves": True,
        "track_deletions": True,
        "chunk_size": 300,
        "chunk_overlap": 50,
        "min_chunk_chars": 50,
        "embedding_model": "fake-model",
        "embedding_batch_size": 8,
        "chroma_path": str(tmp_path / "chroma"),
        "collection_name": "test_collection",
        "ocr_min_chars_per_page": 50,
        "max_retries": 3,
        # OCR off by default so the suite is deterministic on a machine that happens
        # to have Tesseract/key present; OCR tests opt in explicitly.
        "ocr_backend": "none",
        "ocr_dpi": 300,
        "ocr_languages": "eng+kan",
        "ocr_escalate_below_chars": 200,
        "ocr_max_pages_per_file": 500,
        "ocr_cache_path": str(tmp_path / "ocr_cache"),
        "ocr_pdf_path": str(tmp_path / "ocr_pdfs"),
        "ocr_write_searchable_pdfs": True,
        "ocr_api_cost_per_page": 0.0001,
    }
    config.update(overrides)
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(config), encoding="utf-8")
    return path


def update_config(path: Path, **overrides) -> Path:
    """Rewrite a test config in place, e.g. to turn the OCR pass on for a rerun."""
    config = yaml.safe_load(path.read_text(encoding="utf-8"))
    config.update(overrides)
    path.write_text(yaml.safe_dump(config), encoding="utf-8")
    return path


def write_corpus(root: Path, files: dict[str, str]) -> Path:
    for relative, text in files.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    return root


def body(paragraphs: int = 4) -> str:
    return "\n".join(f"Rule {n}.1 Clause {n}.\n"
                     + ("The Commission directs the licensee to file revised accounts. " * 5)
                     for n in range(1, paragraphs + 1))


def run(config: Path, *extra: str) -> int:
    return ingest.main(["--config", str(config), *extra])


def load_manifest(config: Path) -> dict:
    data = json.loads(Path(yaml.safe_load(config.read_text(encoding="utf-8"))["manifest_path"])
                      .read_text(encoding="utf-8"))
    return data["files"]


def scrap(config: Path, relative: str) -> dict:
    return load_manifest(config)[relative]


# --------------------------------------------------------------------------
# Chunking
# --------------------------------------------------------------------------

def test_split_into_blocks_recognises_headings():
    blocks = ingest.split_into_blocks("SECTION 1. Scope\nsome text\nRule 2.1 Fees\nmore text")
    headings = [heading for heading, _ in blocks]
    assert headings[0] == "SECTION 1. Scope"
    assert "Rule 2.1 Fees" in headings


def test_chunk_text_uses_the_label_when_there_is_no_heading():
    chunks = ingest.chunk_text("row one\nrow two\n" + "cell " * 60, "Tariff 2024",
                               {"chunk_size": 300, "chunk_overlap": 50, "min_chunk_chars": 50})
    assert chunks and all(chunk["section"] == "Tariff 2024" for chunk in chunks)


def test_chunk_text_keeps_heading_with_content():
    chunks = ingest.chunk_text("SECTION 1. Scope\n\n" + "word " * 200, None,
                               {"chunk_size": 300, "chunk_overlap": 50, "min_chunk_chars": 50})
    assert chunks, "expected chunks"
    assert chunks[0]["text"].startswith("SECTION 1.")
    assert "word" in chunks[0]["text"], "heading must not be indexed as a chunk of its own"
    assert chunks[0]["section"] == "SECTION 1. Scope"


def test_chunk_text_handles_short_document():
    chunks = ingest.chunk_text("Rule 5. Fee.", None,
                               {"chunk_size": 300, "chunk_overlap": 50, "min_chunk_chars": 50})
    assert [chunk["text"] for chunk in chunks] == ["Rule 5. Fee."]


def test_chunk_text_splits_long_blocks_and_overlaps():
    text = "paragraph without sentence breaks " * 80
    chunks = ingest.chunk_text(text, None,
                               {"chunk_size": 200, "chunk_overlap": 40, "min_chunk_chars": 50})
    assert len(chunks) > 1
    assert all(chunk["text"].strip() for chunk in chunks)
    # Overlap means the tail of one chunk reappears at the head of the next.
    assert chunks[0]["text"][-20:] in chunks[1]["text"]


def test_build_chunks_keeps_page_numbers():
    pages = [{"page": 3, "text": "Rule 7.1 Fees\n" + "text " * 100},
             {"page": 4, "text": "Rule 8.1 Penalties\n" + "text " * 100}]
    chunks = ingest.build_chunks(pages, {"chunk_size": 300, "chunk_overlap": 50,
                                         "min_chunk_chars": 50})
    assert {chunk["page"] for chunk in chunks} == {3, 4}


# --------------------------------------------------------------------------
# Extraction
# --------------------------------------------------------------------------

def test_extract_pages_reads_text_files(tmp_path):
    path = tmp_path / "a.txt"
    path.write_text("hello", encoding="utf-8")
    pages, error = ingest.extract_pages(path, {})
    assert error is None
    assert pages == [{"page": None, "text": "hello"}]


def test_extract_pages_reports_unsupported_format(tmp_path):
    # .doc still needs antiword/libreoffice; the corpus holds four of them.
    path = tmp_path / "a.doc"
    path.write_text("binary-ish", encoding="utf-8")
    pages, error = ingest.extract_pages(path, {})
    assert pages is None
    assert "not implemented" in error


# --------------------------------------------------------------------------
# Spreadsheets
# --------------------------------------------------------------------------

def test_extract_csv_renders_rows_and_skips_blank_lines(tmp_path):
    path = tmp_path / "tariff.csv"
    path.write_text("head,value\n\n2023,45.6\n,\n2024,48.1\n", encoding="utf-8")

    pages, note = ingest.extract_pages(path, {})

    assert note is None
    assert pages == [{"page": None, "label": None,
                      "text": "head | value\n2023 | 45.6\n2024 | 48.1"}]


def test_extract_csv_respects_row_cap(tmp_path):
    path = tmp_path / "big.csv"
    path.write_text("\n".join(f"row{index},x" for index in range(20)), encoding="utf-8")

    pages, note = ingest.extract_pages(path, {"spreadsheet_max_rows": 5})

    assert len(pages[0]["text"].splitlines()) == 5
    assert "first 5 rows kept, 15 dropped" in note


def test_extract_xlsx_uses_one_page_per_sheet(tmp_path):
    openpyxl = pytest.importorskip("openpyxl")
    path = tmp_path / "tariff.xlsx"
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "Tariff 2024"
    sheet.append(["Sl no", "Rate"])
    sheet.append([1, 4.25])
    second = workbook.create_sheet("True-up")
    second.append(["FY", "Amount"])
    workbook.save(path)

    pages, note = ingest.extract_pages(path, {})

    assert note is None
    assert [page["label"] for page in pages] == ["Tariff 2024", "True-up"]
    assert pages[0]["text"] == "Sl no | Rate\n1 | 4.25"
    assert all(page["page"] is None for page in pages)


def test_extract_xlsx_without_openpyxl_is_unsupported(tmp_path, monkeypatch):
    monkeypatch.setattr(ingest, "openpyxl", None)
    path = tmp_path / "a.xlsx"
    path.write_text("not really a workbook", encoding="utf-8")

    pages, error = ingest.extract_pages(path, {})

    assert pages is None
    assert "openpyxl not installed" in error


def test_extract_xls_without_xlrd_is_unsupported(tmp_path, monkeypatch):
    monkeypatch.setattr(ingest, "xlrd", None)
    path = tmp_path / "legacy.xls"
    path.write_text("not really a workbook", encoding="utf-8")

    pages, error = ingest.extract_pages(path, {})

    assert pages is None
    assert "xlrd not installed" in error


def test_extract_xls_reads_sheets_through_xlrd(tmp_path, monkeypatch):
    class FakeSheet:
        def __init__(self, name, rows):
            self.name, self._rows = name, rows

        @property
        def nrows(self):
            return len(self._rows)

        def row_values(self, index):
            return self._rows[index]

    class FakeWorkbook:
        def __init__(self):
            self._sheets = [FakeSheet("Sheet1", [["a", "b"], [1, 2]]),
                            FakeSheet("Empty", [[None, ""]])]

        def sheet_names(self):
            return [sheet.name for sheet in self._sheets]

        def sheet_by_name(self, name):
            return next(sheet for sheet in self._sheets if sheet.name == name)

        def release_resources(self):
            self.released = True

    monkeypatch.setattr(ingest, "xlrd", types.SimpleNamespace(open_workbook=lambda *a, **k: FakeWorkbook()))
    path = tmp_path / "legacy.xls"
    path.write_text("stub", encoding="utf-8")

    pages, note = ingest.extract_pages(path, {})

    assert note is None
    assert [(page["label"], page["text"]) for page in pages] == [("Sheet1", "a | b\n1 | 2")]


def test_spreadsheet_sheet_name_becomes_the_chunk_section(tmp_path, store):
    collection, _ = store
    openpyxl = pytest.importorskip("openpyxl")
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "Tariff 2024"
    sheet.append(["Sl no", "Particulars"])
    for row in range(1, 40):
        sheet.append([row, "Tariff schedule line " * 6])
    workbook.save(corpus / "tariff.xlsx")
    config = write_config(tmp_path, corpus, file_types=["xlsx"])

    run(config)

    assert collection.count() > 0
    assert {entry["metadata"]["section"] for entry in collection.docs.values()
            if entry["metadata"]["section"]} == {"Tariff 2024"}
    entry = scrap(config, "tariff.xlsx")
    assert entry["status"] == ingest.INDEXED
    assert entry["chunk_count"] == collection.count()
    # Spreadsheets have no pages, so citations fall back to file + sheet name.
    assert all(entry["metadata"]["page"] == -1 for entry in collection.docs.values())
    assert all(entry["metadata"]["file_type"] == "xlsx"
               for entry in collection.docs.values())


def test_extract_pages_reports_errors_without_raising(tmp_path, monkeypatch):
    path = tmp_path / "a.txt"
    path.write_text("content", encoding="utf-8")

    def explode(*_args, **_kwargs):
        raise ValueError("boom")

    monkeypatch.setattr(Path, "read_text", explode)
    pages, error = ingest.extract_pages(path, {})
    assert pages == []
    assert "ValueError: boom" in error


def test_has_text_layer_threshold():
    config = {"ocr_min_chars_per_page": 50}
    assert ingest.has_text_layer([{"page": 1, "text": "x" * 60}], config) is True
    assert ingest.has_text_layer([{"page": 1, "text": "x" * 10}], config) is False


# --------------------------------------------------------------------------
# Classification
# --------------------------------------------------------------------------

def entries(*specs) -> dict:
    return {path: {"content_hash": content, "status": ingest.INDEXED, **extra}
            for path, content, extra in specs}


def test_classify_detects_each_change_type():
    config = {"track_moves": True, "track_deletions": True, "max_retries": 3}
    previous = entries(("same.txt", "h1", {}), ("edited.txt", "h2", {}),
                       ("moved_from.txt", "h3", {}), ("gone.txt", "h4", {}))
    current = entries(("same.txt", "h1", {}), ("edited.txt", "h2-edited", {}),
                      ("moved_to.txt", "h3", {}))
    changes = {change["path"]: change for change in ingest.classify(current, previous, config, True)}
    assert changes["same.txt"]["type"] == ingest.UNCHANGED
    assert changes["edited.txt"]["type"] == ingest.MODIFIED
    assert changes["moved_to.txt"]["type"] == ingest.MOVED
    assert changes["moved_to.txt"]["old_path"] == "moved_from.txt"
    assert changes["gone.txt"]["type"] == ingest.DELETED


def test_classify_does_not_call_a_move_a_new_file_when_moves_disabled():
    config = {"track_moves": False, "track_deletions": True, "max_retries": 3}
    previous = entries(("a.txt", "h1", {}))
    current = entries(("b.txt", "h1", {}))
    changes = {change["path"]: change for change in ingest.classify(current, previous, config, True)}
    assert changes["b.txt"]["type"] == ingest.NEW
    assert changes["a.txt"]["type"] == ingest.DELETED


def test_classify_keeps_entry_when_deletions_not_tracked():
    config = {"track_moves": True, "track_deletions": False, "max_retries": 3}
    changes = ingest.classify({}, entries(("gone.txt", "h1", {})), config, True)
    assert changes[0]["type"] == ingest.UNCHANGED
    assert changes[0]["keep"] is True


def test_classify_retries_unfinished_files():
    config = {"track_moves": True, "track_deletions": True, "max_retries": 3}
    previous = entries(("pending.txt", "h1", {"status": ingest.PENDING_EMBEDDING}))
    current = entries(("pending.txt", "h1", {"status": ingest.PENDING_EMBEDDING}))
    changes = ingest.classify(current, previous, config, True)
    assert changes[0]["type"] == ingest.MODIFIED
    assert "pending_embedding" in changes[0]["reason"]


def test_classify_does_not_retry_needs_ocr_while_ocr_is_missing(monkeypatch):
    monkeypatch.setattr(ingest, "OCR_AVAILABLE", False)
    config = {"track_moves": True, "track_deletions": True, "max_retries": 3}
    previous = entries(("scan.pdf", "h1", {"status": ingest.NEEDS_OCR}))
    changes = ingest.classify(entries(("scan.pdf", "h1", {"status": ingest.NEEDS_OCR})),
                              previous, config, True)
    assert changes[0]["type"] == ingest.UNCHANGED

    monkeypatch.setattr(ingest, "OCR_AVAILABLE", True)
    changes = ingest.classify(entries(("scan.pdf", "h1", {"status": ingest.NEEDS_OCR})),
                              previous, config, True)
    assert changes[0]["type"] == ingest.MODIFIED


def test_classify_stops_retrying_after_max_attempts():
    config = {"track_moves": True, "track_deletions": True, "max_retries": 3}
    previous = entries(("broken.txt", "h1", {"status": ingest.ERROR, "attempts": 3}))
    changes = ingest.classify(entries(("broken.txt", "h1", {"status": ingest.ERROR, "attempts": 3})),
                              previous, config, True)
    assert changes[0]["type"] == ingest.UNCHANGED
    assert ingest.needs_reprocessing(previous["broken.txt"], True, 5) == (
        "retry: previous run ended in error")


# --------------------------------------------------------------------------
# Scanning
# --------------------------------------------------------------------------

def test_scan_skips_ignore_list_and_hashes_rest(tmp_path):
    corpus = write_corpus(tmp_path / "corpus", {"keep/a.txt": body(), "dupes/a.txt": body()})
    (tmp_path / "ignore.json").write_text(json.dumps(["dupes/a.txt"]), encoding="utf-8")
    config = yaml.safe_load(write_config(tmp_path, corpus).read_text(encoding="utf-8"))

    current, counters = ingest.scan_sources(config, {}, full_hash=False)

    assert set(current) == {"keep/a.txt"}
    assert counters["ignored"] == 1
    assert counters["hashed"] == 1
    assert counters["scanned"] == 2


def test_scan_matches_ignore_entries_written_with_windows_separators(tmp_path):
    # create_deduplication_ignore_list_v2.py writes str(relative_to(source)), which on
    # Windows contains backslashes.
    corpus = write_corpus(tmp_path / "corpus", {"keep/a.txt": body(), "dupes/a.txt": body()})
    (tmp_path / "ignore.json").write_text(json.dumps(["dupes\\a.txt"]), encoding="utf-8")
    config = yaml.safe_load(write_config(tmp_path, corpus).read_text(encoding="utf-8"))

    current, counters = ingest.scan_sources(config, {}, full_hash=False)

    assert set(current) == {"keep/a.txt"}
    assert counters["ignored"] == 1
    assert counters["stale_ignores"] == []


def test_scan_matches_absolute_ignore_entries(tmp_path):
    corpus = write_corpus(tmp_path / "corpus", {"dupes/a.txt": body()})
    (tmp_path / "ignore.json").write_text(
        json.dumps([str(corpus / "dupes" / "a.txt")]), encoding="utf-8")
    config = yaml.safe_load(write_config(tmp_path, corpus).read_text(encoding="utf-8"))

    current, counters = ingest.scan_sources(config, {}, full_hash=False)

    assert current == {}
    assert counters["ignored"] == 1
    assert counters["stale_ignores"] == []


def test_scan_reports_stale_ignore_entries(tmp_path):
    corpus = write_corpus(tmp_path / "corpus", {"a.txt": body()})
    (tmp_path / "ignore.json").write_text(json.dumps(["vanished.txt"]), encoding="utf-8")
    config = yaml.safe_load(write_config(tmp_path, corpus).read_text(encoding="utf-8"))

    _, counters = ingest.scan_sources(config, {}, full_hash=False)

    assert counters["stale_ignores"] == ["vanished.txt"]


def test_scan_reuses_previous_entry_when_size_and_mtime_match(tmp_path, monkeypatch):
    corpus = write_corpus(tmp_path / "corpus", {"a.txt": body()})
    config = yaml.safe_load(write_config(tmp_path, corpus).read_text(encoding="utf-8"))
    current, _ = ingest.scan_sources(config, {}, full_hash=False)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("hash_file should not run for an unchanged file")

    monkeypatch.setattr(ingest, "hash_file", forbidden)
    again, counters = ingest.scan_sources(config, current, full_hash=False)

    assert counters["hashed"] == 0
    assert again["a.txt"]["content_hash"] == current["a.txt"]["content_hash"]


def test_scan_records_unreadable_file(tmp_path, monkeypatch):
    corpus = write_corpus(tmp_path / "corpus", {"a.txt": body()})
    config = yaml.safe_load(write_config(tmp_path, corpus).read_text(encoding="utf-8"))

    def explode(*_args, **_kwargs):
        raise OSError("locked")

    monkeypatch.setattr(ingest, "hash_file", explode)
    current, counters = ingest.scan_sources(config, {}, full_hash=False)

    assert counters["unreadable"] == 1
    assert current["a.txt"]["status"] == ingest.ERROR
    assert "locked" in current["a.txt"]["note"]


def test_scan_deduplicates_overlapping_sources(tmp_path):
    corpus = write_corpus(tmp_path / "corpus", {"sub/a.txt": body()})
    config = yaml.safe_load(write_config(tmp_path, corpus, docs_source=[
        str(corpus), str(corpus / "sub")]).read_text(encoding="utf-8"))

    current, counters = ingest.scan_sources(config, {}, full_hash=False)

    assert set(current) == {"sub/a.txt"}
    assert counters["overlap"] == 1
    assert counters["nested_sources"] == [(str(corpus), str(corpus / "sub"))]


def test_scan_keeps_entries_of_unreachable_source(tmp_path):
    corpus = write_corpus(tmp_path / "corpus", {"a.txt": body()})
    config = yaml.safe_load(write_config(tmp_path, corpus).read_text(encoding="utf-8"))
    current, _ = ingest.scan_sources(config, {}, full_hash=False)

    moved_away = corpus.rename(tmp_path / "corpus_offline")
    try:
        offline, counters = ingest.scan_sources(config, current, full_hash=False)
    finally:
        moved_away.rename(corpus)

    assert set(offline) == {"a.txt"}, "an offline source must not look like a mass deletion"
    assert counters["kept_offline"] == 1
    assert counters["unreachable"] == [str(corpus)]


def test_scan_groups_duplicate_content(tmp_path):
    corpus = write_corpus(tmp_path / "corpus", {"one/a.txt": body(), "two/a.txt": body()})
    config = yaml.safe_load(write_config(tmp_path, corpus).read_text(encoding="utf-8"))

    _, counters = ingest.scan_sources(config, {}, full_hash=False)

    assert counters["duplicate_groups"] == [["one/a.txt", "two/a.txt"]]


def test_scan_skips_office_lock_files(tmp_path):
    # ~$*.docx (Word/Excel) and .~lock.*# (LibreOffice) are transient owner files, not
    # corpus content: they must not enter the manifest as retryable `error` entries.
    corpus = write_corpus(tmp_path / "corpus", {
        "a.txt": body(),
        "~$VER PAGE.docx": "",
        "sub/.~lock.notes.odt#": "",
    })
    config = yaml.safe_load(write_config(tmp_path, corpus).read_text(encoding="utf-8"))

    current, counters = ingest.scan_sources(config, {}, full_hash=False)

    assert set(current) == {"a.txt"}
    assert counters["lock_files"] == 2
    assert counters["unreadable"] == 0


def test_scan_lock_patterns_come_from_config(tmp_path):
    corpus = write_corpus(tmp_path / "corpus", {"a.txt": body(), "draft.tmp.txt": body()})
    config = yaml.safe_load(write_config(
        tmp_path, corpus, lock_file_patterns=["*.tmp.txt"]).read_text(encoding="utf-8"))

    current, counters = ingest.scan_sources(config, {}, full_hash=False)

    assert set(current) == {"a.txt"}
    assert counters["lock_files"] == 1


def test_is_lock_file_ignores_normal_names():
    assert ingest.is_lock_file("~$VER PAGE.docx", ingest.DEFAULT_LOCK_FILE_PATTERNS)
    assert ingest.is_lock_file(".~lock.notes.odt#", ingest.DEFAULT_LOCK_FILE_PATTERNS)
    assert not ingest.is_lock_file("VER PAGE.docx", ingest.DEFAULT_LOCK_FILE_PATTERNS)
    assert not ingest.is_lock_file("notes.odt", ingest.DEFAULT_LOCK_FILE_PATTERNS)


# --------------------------------------------------------------------------
# OCR pass
# --------------------------------------------------------------------------

class FakeOcrEngine:
    """Stand-in engine: returns canned text and counts calls."""

    def __init__(self, name="tesseract", text="OCR text. " * 20, available=True, fail=False):
        self.name = name
        self.text = text
        self.calls = 0
        self._available = available
        self.fail = fail

    def available(self):
        return self._available

    def ocr_image(self, image):
        self.calls += 1
        if self.fail:
            raise RuntimeError("engine exploded")
        return self.text


def ocr_config(tmp_path: Path, **overrides) -> dict:
    config = {
        "ocr_backend": "hybrid",
        "ocr_dpi": 300,
        "ocr_languages": "eng+kan",
        "ocr_min_chars_per_page": 50,
        "ocr_escalate_below_chars": 200,
        "ocr_max_pages_per_file": 500,
        "ocr_cache_path": str(tmp_path / "ocr_cache"),
        "ocr_pdf_path": str(tmp_path / "ocr_pdfs"),
        "ocr_write_searchable_pdfs": True,
        "ocr_api_cost_per_page": 0.0001,
    }
    config.update(overrides)
    return config


def make_runner(tmp_path: Path, engines, total_limit=None, **overrides) -> "ingest.OcrRunner":
    return ingest.OcrRunner(ocr_config(tmp_path, **overrides), engines, total_limit=total_limit)


def fake_pdf(monkeypatch, page_count: int = 1) -> None:
    """Replace pdfplumber's opener and the renderer so OCR needs neither on disk."""
    class FakePdf:
        def __init__(self):
            self.pages = [object() for _ in range(page_count)]

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

    monkeypatch.setattr(ingest, "pdfplumber",
                        types.SimpleNamespace(open=lambda path: FakePdf()))
    monkeypatch.setattr(ingest, "render_pdf_page", lambda page, config: ("image", None))


def test_build_ocr_engines_by_backend():
    def names(backend):
        return [engine.name for engine in ingest.build_ocr_engines({"ocr_backend": backend})]

    assert names("none") == []
    assert names("off") == []
    assert names("disabled") == []
    assert names("tesseract") == ["tesseract"]
    assert names("novita") == ["novita"]
    assert names("hybrid") == ["tesseract", "novita"]


def test_tesseract_engine_requires_every_configured_language(monkeypatch):
    monkeypatch.setattr(ingest, "pytesseract", types.SimpleNamespace(
        get_languages=lambda config="": ["eng"],
        image_to_string=lambda image, lang: "text"))

    assert ingest.TesseractEngine({"ocr_languages": "eng"}).available() is True
    strict = ingest.TesseractEngine({"ocr_languages": "eng+kan"})
    assert strict.available() is False
    assert strict.missing_languages() == ["kan"]

    monkeypatch.setattr(ingest, "pytesseract", None)
    assert ingest.TesseractEngine({"ocr_languages": "eng"}).available() is False


def test_tesseract_engine_is_unavailable_without_the_binary(monkeypatch):
    def no_binary(config=""):
        raise RuntimeError("tesseract is not installed or it's not in your PATH")

    monkeypatch.setattr(ingest, "pytesseract", types.SimpleNamespace(get_languages=no_binary))

    assert ingest.TesseractEngine({"ocr_languages": "eng"}).available() is False


def test_novita_engine_needs_an_api_key(monkeypatch):
    monkeypatch.delenv("NOVITA_API_KEY", raising=False)
    assert ingest.NovitaEngine({}).available() is False

    monkeypatch.setenv("NOVITA_API_KEY", "secret")
    assert ingest.NovitaEngine({}).available() is True
    assert ingest.NovitaEngine({"ocr_api_key_env": "OTHER_KEY"}).available() is False


def test_novita_engine_posts_the_page_and_reads_the_reply(monkeypatch):
    monkeypatch.setenv("NOVITA_API_KEY", "secret")
    captured: dict = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

        def read(self):
            return json.dumps({"choices": [{"message": {"content": "Recovered"}}]}).encode()

    def fake_urlopen(request, timeout=None):
        captured["url"] = request.full_url
        captured["headers"] = request.headers
        captured["body"] = json.loads(request.data.decode("utf-8"))
        captured["timeout"] = timeout
        return Response()

    monkeypatch.setattr(ingest.urllib.request, "urlopen", fake_urlopen)

    class FakeImage:
        def save(self, buffer, format=None):
            buffer.write(b"png-bytes")

    text = ingest.NovitaEngine({"ocr_api_timeout": 5}).ocr_image(FakeImage())

    assert text == "Recovered"
    assert captured["url"] == ingest.DEFAULT_OCR_API_URL
    assert captured["timeout"] == 5
    assert captured["headers"]["Authorization"] == "Bearer secret"
    assert captured["body"]["model"] == ingest.DEFAULT_OCR_API_MODEL
    content = captured["body"]["messages"][0]["content"]
    assert content[1]["image_url"]["url"].startswith("data:image/png;base64,")


def test_extract_api_text_handles_response_shapes():
    assert ingest.extract_api_text({"choices": [{"message": {"content": "hi"}}]}) == "hi"
    assert ingest.extract_api_text({"choices": [{"message": {"content": [
        {"text": "a"}, {"text": "b"}]}}]}) == "ab"
    assert ingest.extract_api_text({}) == ""
    assert ingest.extract_api_text({"choices": []}) == ""
    assert ingest.extract_api_text("not a dict") == ""


def test_ocr_fingerprint_tracks_settings_and_engines():
    base = ingest.ocr_fingerprint({"ocr_backend": "hybrid"}, ["tesseract"])

    assert base == ingest.ocr_fingerprint({"ocr_backend": "hybrid"}, ["tesseract"])
    # A newly usable engine means the file deserves another attempt.
    assert base != ingest.ocr_fingerprint({"ocr_backend": "hybrid"}, ["tesseract", "novita"])
    assert base != ingest.ocr_fingerprint({"ocr_backend": "tesseract"}, ["tesseract"])
    assert base != ingest.ocr_fingerprint({"ocr_backend": "hybrid", "ocr_dpi": 400},
                                           ["tesseract"])
    # A different endpoint/model produces different text, so cached text must not be
    # reused after either changes.
    assert base != ingest.ocr_fingerprint({"ocr_backend": "hybrid",
                                           "ocr_api_model": "other/model"}, ["tesseract"])


def test_render_pdf_page_uses_the_config_dpi(monkeypatch):
    calls: dict = {}

    class Page:
        def to_image(self, resolution):
            calls["resolution"] = resolution
            return types.SimpleNamespace(original="image")

    image, error = ingest.render_pdf_page(Page(), {"ocr_dpi": 150})
    assert (image, error) == ("image", None)
    assert calls["resolution"] == 150

    class Broken:
        def to_image(self, resolution):
            raise RuntimeError("pypdfium2 missing")

    image, error = ingest.render_pdf_page(Broken(), {})
    assert image is None
    assert "render failed" in error


def test_ocr_fill_only_touches_pages_without_text(tmp_path, monkeypatch):
    fake_pdf(monkeypatch, page_count=2)
    engine = FakeOcrEngine(text="Recovered text " * 10)
    runner = make_runner(tmp_path, [engine])
    pages = [{"page": 1, "text": "Rule 1.1 Fees\n" + "text " * 60},
             {"page": 2, "text": ""}]

    result = runner.fill("a.pdf", tmp_path / "a.pdf", pages,
                         {"root": str(tmp_path), "content_hash": "h1"})

    assert pages[0]["text"].startswith("Rule 1.1"), "a page with text must be left alone"
    assert pages[1]["text"] == "Recovered text " * 10
    assert result == {**result, "ocr_pages": 1, "attempted": True,
                      "incomplete": False, "note": None}
    assert engine.calls == 1


def test_ocr_fill_does_nothing_without_candidates(tmp_path, monkeypatch):
    fake_pdf(monkeypatch)
    engine = FakeOcrEngine()
    runner = make_runner(tmp_path, [engine])
    pages = [{"page": 1, "text": "plenty of text " * 20}]

    result = runner.fill("a.pdf", tmp_path / "a.pdf", pages,
                         {"root": str(tmp_path), "content_hash": "h1"})

    assert result["attempted"] is False
    assert engine.calls == 0


def test_ocr_cache_avoids_rerunning_the_engine(tmp_path, monkeypatch):
    fake_pdf(monkeypatch)
    engine = FakeOcrEngine(text="Cached text " * 20)
    runner = make_runner(tmp_path, [engine])
    entry = {"root": str(tmp_path), "content_hash": "h1"}
    runner.fill("a.pdf", tmp_path / "a.pdf", [{"page": 1, "text": ""}], entry)
    runner.flush()
    assert engine.calls == 1

    again = FakeOcrEngine()
    second = make_runner(tmp_path, [again])
    pages = [{"page": 1, "text": ""}]
    result = second.fill("a.pdf", tmp_path / "a.pdf", pages, entry)

    assert pages[0]["text"] == "Cached text " * 20
    assert again.calls == 0
    assert second.cache_hits == 1
    assert result["ocr_pages"] == 0


def test_ocr_cache_is_discarded_when_the_content_changes(tmp_path, monkeypatch):
    fake_pdf(monkeypatch)
    engine = FakeOcrEngine(text="Cached text " * 20)
    runner = make_runner(tmp_path, [engine])
    runner.fill("a.pdf", tmp_path / "a.pdf", [{"page": 1, "text": ""}],
                {"root": str(tmp_path), "content_hash": "h1"})
    runner.flush()

    second_engine = FakeOcrEngine(text="Fresh text " * 20)
    second = make_runner(tmp_path, [second_engine])
    pages = [{"page": 1, "text": ""}]
    second.fill("a.pdf", tmp_path / "a.pdf", pages, {"root": str(tmp_path),
                                                     "content_hash": "h2"})

    assert second_engine.calls == 1
    assert pages[0]["text"] == "Fresh text " * 20


def test_ocr_cache_is_discarded_when_the_settings_change(tmp_path, monkeypatch):
    fake_pdf(monkeypatch)
    entry = {"root": str(tmp_path), "content_hash": "h1"}
    runner = make_runner(tmp_path, [FakeOcrEngine()])
    runner.fill("a.pdf", tmp_path / "a.pdf", [{"page": 1, "text": ""}], entry)
    runner.flush()

    # Different languages (part of the fingerprint) must not reuse the old text.
    engine = FakeOcrEngine()
    second = make_runner(tmp_path, [engine], ocr_languages="eng")
    second.fill("a.pdf", tmp_path / "a.pdf", [{"page": 1, "text": ""}], entry)

    assert engine.calls == 1


def test_ocr_limit_defers_the_remaining_pages(tmp_path, monkeypatch):
    fake_pdf(monkeypatch, page_count=3)
    engine = FakeOcrEngine()
    runner = make_runner(tmp_path, [engine], total_limit=1)
    pages = [{"page": number, "text": ""} for number in (1, 2, 3)]

    result = runner.fill("a.pdf", tmp_path / "a.pdf", pages,
                         {"root": str(tmp_path), "content_hash": "h1"})

    assert result["ocr_pages"] == 1
    assert result["incomplete"] is True
    assert engine.calls == 1
    assert runner.deferred == 2


def test_ocr_per_file_cap_is_reported_and_not_settled(tmp_path, monkeypatch):
    fake_pdf(monkeypatch, page_count=4)
    engine = FakeOcrEngine()
    runner = make_runner(tmp_path, [engine], ocr_max_pages_per_file=2)
    pages = [{"page": number, "text": ""} for number in range(1, 5)]

    result = runner.fill("a.pdf", tmp_path / "a.pdf", pages,
                         {"root": str(tmp_path), "content_hash": "h1"})

    assert result["ocr_pages"] == 2
    assert result["incomplete"] is True
    assert "capped at 2 page(s)" in result["note"]


def test_ocr_reports_pages_it_could_not_render(tmp_path, monkeypatch):
    fake_pdf(monkeypatch)
    monkeypatch.setattr(ingest, "render_pdf_page", lambda page, config: (None, "render failed"))
    engine = FakeOcrEngine()
    runner = make_runner(tmp_path, [engine])
    pages = [{"page": 1, "text": ""}]

    result = runner.fill("a.pdf", tmp_path / "a.pdf", pages,
                         {"root": str(tmp_path), "content_hash": "h1"})

    assert result["ocr_pages"] == 0
    assert "could not be rendered" in result["note"]
    assert engine.calls == 0
    assert runner.render_errors == 1


def test_ocr_survives_a_pdf_that_cannot_be_opened(tmp_path, monkeypatch):
    def boom(path):
        raise RuntimeError("encrypted")

    monkeypatch.setattr(ingest, "pdfplumber", types.SimpleNamespace(open=boom))
    runner = make_runner(tmp_path, [FakeOcrEngine()])
    pages = [{"page": 1, "text": ""}]

    result = runner.fill("a.pdf", tmp_path / "a.pdf", pages,
                         {"root": str(tmp_path), "content_hash": "h1"})

    assert result["ocr_pages"] == 0
    assert "cannot open for OCR" in result["note"]
    assert result["fatal"] and "cannot open" in result["fatal"]
    assert pages[0]["text"] == ""


def test_ocr_reports_an_engine_failure_without_crashing(tmp_path, monkeypatch):
    fake_pdf(monkeypatch)
    runner = make_runner(tmp_path, [FakeOcrEngine(fail=True)])
    pages = [{"page": 1, "text": ""}]

    result = runner.fill("a.pdf", tmp_path / "a.pdf", pages,
                         {"root": str(tmp_path), "content_hash": "h1"})

    assert result["ocr_pages"] == 0
    assert "tesseract failed" in result["note"]
    assert result["errors"] == 1
    assert result["recovered"] == 0
    assert runner.ocr_failures == 1


def test_ocr_engine_failure_is_not_cached_as_an_empty_page(tmp_path, monkeypatch):
    fake_pdf(monkeypatch)
    entry = {"root": str(tmp_path), "content_hash": "h1"}
    runner = make_runner(tmp_path, [FakeOcrEngine(fail=True)])

    runner.fill("a.pdf", tmp_path / "a.pdf", [{"page": 1, "text": ""}], entry)
    runner.flush()

    # A page the engine failed to read must not be remembered as "read, and empty",
    # or a transient failure (timeout, restart) would become permanent.
    second_engine = FakeOcrEngine(text="Recovered. " * 20)
    second = make_runner(tmp_path, [second_engine])
    pages = [{"page": 1, "text": ""}]
    result = second.fill("a.pdf", tmp_path / "a.pdf", pages, entry)

    assert second_engine.calls == 1
    assert pages[0]["text"] == "Recovered. " * 20
    assert result["recovered"] == 1


def test_hybrid_escalation_salvages_a_failed_primary_engine(tmp_path, monkeypatch):
    fake_pdf(monkeypatch)
    primary = FakeOcrEngine(name="tesseract", fail=True)
    better = FakeOcrEngine(name="novita", text="Recovered by the API. " * 20)
    runner = make_runner(tmp_path, [primary, better])
    pages = [{"page": 1, "text": ""}]

    result = runner.fill("a.pdf", tmp_path / "a.pdf", pages,
                         {"root": str(tmp_path), "content_hash": "h1"})

    assert primary.calls == 1
    assert better.calls == 1
    assert result["errors"] == 0, "a rescued page is not a failure"
    assert result["recovered"] == 1
    assert pages[0]["text"] == "Recovered by the API. " * 20
    assert runner.api_pages == 1


def test_ocr_limit_counts_pages_that_read_empty(tmp_path, monkeypatch):
    fake_pdf(monkeypatch)
    engine = FakeOcrEngine(text="")          # a successful read that found nothing
    runner = make_runner(tmp_path, [engine], total_limit=1)
    entry = {"root": str(tmp_path), "content_hash": "h1"}

    first = runner.fill("a.pdf", tmp_path / "a.pdf", [{"page": 1, "text": ""}], entry)
    second = runner.fill("b.pdf", tmp_path / "b.pdf", [{"page": 1, "text": ""}], entry)

    # An empty read still spent the run's budget; without counting it, the cap would
    # not bound the page count that was actually sent to a paid engine.
    assert engine.calls == 1
    assert runner.pages_attempted == 1
    assert first["errors"] == 0 and second["recovered"] == 0
    assert second["incomplete"] is True


def test_hybrid_escalates_pages_the_first_engine_could_not_read(tmp_path, monkeypatch):
    fake_pdf(monkeypatch)
    primary = FakeOcrEngine(name="tesseract", text="tiny")
    better = FakeOcrEngine(name="novita", text="Recovered properly " * 20)
    runner = make_runner(tmp_path, [primary, better])
    pages = [{"page": 1, "text": ""}]

    runner.fill("a.pdf", tmp_path / "a.pdf", pages,
                {"root": str(tmp_path), "content_hash": "h1"})

    assert pages[0]["text"] == "Recovered properly " * 20
    assert primary.calls == 1
    assert better.calls == 1
    assert runner.escalations == 1
    assert runner.api_pages == 1


def test_hybrid_does_not_escalate_pages_the_first_engine_read(tmp_path, monkeypatch):
    fake_pdf(monkeypatch)
    primary = FakeOcrEngine(name="tesseract", text="long enough text " * 20)
    better = FakeOcrEngine(name="novita")
    runner = make_runner(tmp_path, [primary, better])

    runner.fill("a.pdf", tmp_path / "a.pdf", [{"page": 1, "text": ""}],
                {"root": str(tmp_path), "content_hash": "h1"})

    assert better.calls == 0
    assert runner.api_pages == 0


def test_ocr_fill_is_a_noop_without_engines(tmp_path):
    runner = make_runner(tmp_path, [])
    pages = [{"page": 1, "text": ""}]

    result = runner.fill("a.pdf", tmp_path / "a.pdf", pages,
                         {"root": str(tmp_path), "content_hash": "h1"})

    assert runner.available() is False
    assert result["attempted"] is False
    assert pages[0]["text"] == ""


def test_searchable_pdf_is_written_once_per_content_and_settings(tmp_path, monkeypatch):
    calls = []

    def fake_ocr(source, destination, **kwargs):
        Path(destination).parent.mkdir(parents=True, exist_ok=True)
        Path(destination).write_bytes(b"%PDF-1.4 fake")
        calls.append((source, destination, kwargs))

    monkeypatch.setattr(ingest, "ocrmypdf", types.SimpleNamespace(ocr=fake_ocr))
    config = ocr_config(tmp_path)
    entry = {"content_hash": "h1"}
    source = tmp_path / "source.pdf"

    written, note = ingest.ensure_searchable_pdf("a.pdf", source, entry, config, "fp1")

    assert (written, note) == (True, None)
    assert calls[0][2]["skip_text"] is True, "only pages without a layer may be OCR'd"
    assert calls[0][2]["language"] == "eng+kan"
    assert (tmp_path / "ocr_pdfs" / "a.pdf").exists()

    again, _ = ingest.ensure_searchable_pdf("a.pdf", source, entry, config, "fp1")
    assert again is False
    assert len(calls) == 1

    entry["content_hash"] = "h2"
    third, _ = ingest.ensure_searchable_pdf("a.pdf", source, entry, config, "fp1")
    assert third is True
    assert len(calls) == 2


def test_searchable_pdf_is_skipped_without_ocrmypdf(tmp_path, monkeypatch):
    monkeypatch.setattr(ingest, "ocrmypdf", None)

    written, note = ingest.ensure_searchable_pdf("a.pdf", tmp_path / "s.pdf", {},
                                                 ocr_config(tmp_path), "fp")

    # Silently skipped here; report_ocr says so once for the whole run instead of
    # writing the same note against every OCR'd file.
    assert (written, note) == (False, None)


def test_searchable_pdf_can_be_turned_off(tmp_path, monkeypatch):
    monkeypatch.setattr(ingest, "ocrmypdf", None)

    written, note = ingest.ensure_searchable_pdf(
        "a.pdf", tmp_path / "s.pdf", {}, ocr_config(tmp_path, ocr_write_searchable_pdfs=False),
        "fp")

    assert (written, note) == (False, None)


def test_classify_leaves_ocr_files_alone_while_no_engine_is_available():
    config = {"track_moves": True, "track_deletions": True, "max_retries": 3}
    previous = entries(("scan.pdf", "h1", {"status": ingest.NEEDS_OCR}))

    changes = ingest.classify(entries(("scan.pdf", "h1", {"status": ingest.NEEDS_OCR})),
                              previous, config, True,
                              {"available": False, "fingerprint": "fp"})

    assert changes[0]["type"] == ingest.UNCHANGED


def test_classify_retries_needs_ocr_until_the_fingerprint_is_recorded():
    config = {"track_moves": True, "track_deletions": True, "max_retries": 3}
    ocr = {"available": True, "fingerprint": "fp"}
    previous = entries(("scan.pdf", "h1", {"status": ingest.NEEDS_OCR}))

    changes = ingest.classify(entries(("scan.pdf", "h1", {"status": ingest.NEEDS_OCR})),
                              previous, config, True, ocr)
    assert changes[0]["type"] == ingest.MODIFIED
    assert changes[0]["reason"] == "retry: OCR available"

    attempted = entries(("scan.pdf", "h1", {"status": ingest.NEEDS_OCR,
                                              "ocr_fingerprint": "fp"}))
    changes = ingest.classify(attempted, attempted, config, True, ocr)
    assert changes[0]["type"] == ingest.UNCHANGED, "a hopeless file must stop costing money"


def test_classify_reprocesses_an_indexed_pdf_with_blank_pages():
    config = {"track_moves": True, "track_deletions": True, "max_retries": 3}
    ocr = {"available": True, "fingerprint": "fp"}
    previous = entries(("mixed.pdf", "h1", {"status": ingest.INDEXED, "pages_without_text": 3}))

    changes = ingest.classify(
        entries(("mixed.pdf", "h1", {"status": ingest.INDEXED, "pages_without_text": 3})),
        previous, config, True, ocr)

    assert changes[0]["type"] == ingest.MODIFIED
    assert "no text layer" in changes[0]["reason"]

    settled = entries(("mixed.pdf", "h1", {"status": ingest.INDEXED, "pages_without_text": 0,
                                             "ocr_fingerprint": "fp"}))
    changes = ingest.classify(settled, settled, config, True, ocr)
    assert changes[0]["type"] == ingest.UNCHANGED


def test_classify_ignores_blank_pages_in_non_pdf_files():
    config = {"track_moves": True, "track_deletions": True, "max_retries": 3}
    ocr = {"available": True, "fingerprint": "fp"}
    previous = entries(("note.txt", "h1", {"status": ingest.INDEXED, "pages_without_text": 1}))

    changes = ingest.classify(
        entries(("note.txt", "h1", {"status": ingest.INDEXED, "pages_without_text": 1})),
        previous, config, True, ocr)

    assert changes[0]["type"] == ingest.UNCHANGED, "only PDFs are OCR candidates"


def test_embedding_fingerprint_tracks_the_model():
    base = ingest.embedding_fingerprint({"embedding_model": "all-MiniLM-L6-v2"})

    assert base == ingest.embedding_fingerprint({"embedding_model": "all-MiniLM-L6-v2"})
    assert base != ingest.embedding_fingerprint({"embedding_model": "bge-m3"})


def test_classify_reembeds_when_the_embedding_model_changes():
    config = {"track_moves": True, "track_deletions": True, "max_retries": 3,
              "embedding_model": "bge-m3"}
    entry = {"status": ingest.INDEXED, "embedding_fingerprint": "oldmodel1234"}

    changes = ingest.classify(entries(("a.txt", "h1", entry)), entries(("a.txt", "h1", entry)),
                              config, True)

    assert changes[0]["type"] == ingest.MODIFIED
    assert "embedding model changed" in changes[0]["reason"]


def test_classify_keeps_files_when_the_embedding_model_is_unchanged():
    config = {"track_moves": True, "track_deletions": True, "max_retries": 3,
              "embedding_model": "bge-m3"}
    entry = {"status": ingest.INDEXED, "embedding_fingerprint": ingest.embedding_fingerprint(config)}

    changes = ingest.classify(entries(("a.txt", "h1", entry)), entries(("a.txt", "h1", entry)),
                              config, True)

    assert changes[0]["type"] == ingest.UNCHANGED


def test_classify_does_not_reembed_without_the_embedding_stack():
    config = {"track_moves": True, "track_deletions": True, "max_retries": 3,
              "embedding_model": "bge-m3"}
    entry = {"status": ingest.INDEXED, "embedding_fingerprint": "oldmodel1234"}

    changes = ingest.classify(entries(("a.txt", "h1", entry)), entries(("a.txt", "h1", entry)),
                              config, False)

    assert changes[0]["type"] == ingest.UNCHANGED, "no re-embed loop while deps are missing"


def test_scan_backfills_an_embedding_fingerprint(tmp_path):
    """A manifest written before the field existed must not trigger a full re-embed."""
    corpus = write_corpus(tmp_path / "corpus", {"a.txt": body()})
    config = yaml.safe_load(write_config(tmp_path, corpus).read_text(encoding="utf-8"))
    stat = (corpus / "a.txt").stat()
    previous = {"a.txt": {"content_hash": "h1", "status": ingest.INDEXED,
                           "size": stat.st_size, "mtime": stat.st_mtime, "root": str(corpus)}}

    current, _ = ingest.scan_sources(config, previous, full_hash=False)

    assert current["a.txt"]["status"] == ingest.INDEXED
    assert current["a.txt"]["embedding_fingerprint"] == ingest.embedding_fingerprint(config)


# --------------------------------------------------------------------------
# End-to-end runs
# --------------------------------------------------------------------------

def test_first_run_indexes_and_writes_manifest(tmp_path, store, capsys):
    collection, counters = store
    corpus = write_corpus(tmp_path / "corpus", {"orders/rule14.txt": body()})
    config = write_config(tmp_path, corpus)

    assert run(config) == 0

    assert collection.count() > 0
    assert collection.sources() == {"orders/rule14.txt"}
    assert scrap(config, "orders/rule14.txt")["status"] == ingest.INDEXED
    assert counters["embedded"] == collection.count()
    assert "Indexed:" in capsys.readouterr().out


def test_second_run_is_a_no_op(tmp_path, store):
    collection, counters = store
    corpus = write_corpus(tmp_path / "corpus", {"a.txt": body()})
    config = write_config(tmp_path, corpus)
    run(config)
    before, embedded = collection.count(), counters["embedded"]

    run(config)

    assert collection.count() == before
    assert counters["embedded"] == embedded, "unchanged files must not be re-embedded"


def test_edit_replaces_the_old_chunks(tmp_path, store):
    collection, _ = store
    corpus = write_corpus(tmp_path / "corpus", {"a.txt": body(4)})
    config = write_config(tmp_path, corpus)
    run(config)
    first_count = collection.count()

    (corpus / "a.txt").write_text(body(8), encoding="utf-8")
    run(config)

    assert collection.count() > first_count
    assert collection.sources() == {"a.txt"}
    assert ("upsert", collection.count()) in collection.calls
    assert scrap(config, "a.txt")["chunk_count"] == collection.count()


def test_move_relabels_chunks_without_embedding(tmp_path, store):
    collection, counters = store
    corpus = write_corpus(tmp_path / "corpus", {"a.txt": body()})
    config = write_config(tmp_path, corpus)
    run(config)
    embedded_before = counters["embedded"]

    (corpus / "renamed.txt").unlink(missing_ok=True)
    (corpus / "a.txt").rename(corpus / "renamed.txt")
    assert run(config) == 0

    assert counters["embedded"] == embedded_before, "a move must not re-embed"
    assert collection.sources() == {"renamed.txt"}
    assert any(call[0] == "update" for call in collection.calls)
    assert scrap(config, "renamed.txt")["status"] == ingest.INDEXED
    assert "a.txt" not in load_manifest(config)


def test_delete_removes_chunks(tmp_path, store):
    collection, _ = store
    corpus = write_corpus(tmp_path / "corpus", {"a.txt": body(), "b.txt": body()})
    config = write_config(tmp_path, corpus)
    run(config)

    (corpus / "a.txt").unlink()
    run(config)

    assert collection.sources() == {"b.txt"}
    assert "a.txt" not in load_manifest(config)


def test_new_subfolder_is_picked_up(tmp_path, store):
    collection, _ = store
    corpus = write_corpus(tmp_path / "corpus", {"a.txt": body()})
    config = write_config(tmp_path, corpus)
    run(config)

    write_corpus(corpus, {"2024/orders/c.pdf.txt": body()})
    run(config)

    assert collection.sources() == {"a.txt", "2024/orders/c.pdf.txt"}


def test_dry_run_writes_nothing(tmp_path, store, capsys):
    collection, _ = store
    corpus = write_corpus(tmp_path / "corpus", {"a.txt": body()})
    config = write_config(tmp_path, corpus)

    assert run(config, "--dry-run") == 0

    assert collection.count() == 0
    assert not Path(yaml.safe_load(config.read_text(encoding="utf-8"))["manifest_path"]).exists()
    out = capsys.readouterr().out
    assert "Dry run: nothing written" in out
    assert "would NEW" in out


def test_touched_file_is_unchanged_and_not_reembedded(tmp_path, store):
    collection, counters = store
    corpus = write_corpus(tmp_path / "corpus", {"a.txt": body()})
    config = write_config(tmp_path, corpus)
    run(config)
    embedded = counters["embedded"]

    path = corpus / "a.txt"
    path.touch()  # mtime changes, content does not
    run(config)

    assert counters["embedded"] == embedded
    assert scrap(config, "a.txt")["status"] == ingest.INDEXED


def test_case_only_rename_is_a_move(tmp_path, store):
    collection, counters = store
    corpus = write_corpus(tmp_path / "corpus", {"Rule14.txt": body()})
    config = write_config(tmp_path, corpus)
    run(config)
    embedded = counters["embedded"]

    (corpus / "Rule14.txt").rename(corpus / "rule14.txt")
    run(config)

    assert counters["embedded"] == embedded
    assert collection.sources() == {"rule14.txt"}


def test_audit_extracts_and_reports_without_writing(tmp_path, store, capsys, monkeypatch):
    collection, counters = store
    corpus = write_corpus(tmp_path / "corpus", {"a.txt": body(), "scan.pdf": "x"})
    config = write_config(tmp_path, corpus)
    monkeypatch.setattr(ingest, "extract_pages", lambda path, config: (
        ([{"page": 1, "text": ""}] if path.suffix == ".pdf"
         else [{"page": None, "text": body()}]), None))

    assert run(config, "--audit") == 0

    assert collection.count() == 0, "audit must not write to the vector store"
    assert counters["embedded"] == 0, "audit must not embed"
    assert not (config.parent / "manifest.json").exists(), "audit must not write a manifest"
    out = capsys.readouterr().out
    assert "Mode: audit" in out
    assert "Would index: 1 file(s)" in out
    assert "needs_ocr" in out
    assert "Audit: nothing written" in out


def test_audit_can_run_without_embedding_dependencies(tmp_path, no_deps, capsys):
    corpus = write_corpus(tmp_path / "corpus", {"a.txt": body()})
    config = write_config(tmp_path, corpus)

    assert run(config, "--audit") == 0

    out = capsys.readouterr().out
    assert "Would index: 1 file(s)" in out
    assert "extraction only" in out


def test_offline_source_does_not_wipe_the_collection(tmp_path, store, capsys):
    collection, _ = store
    corpus = write_corpus(tmp_path / "corpus", {"a.txt": body()})
    config = write_config(tmp_path, corpus)
    run(config)
    before = collection.count()

    offline = corpus.rename(tmp_path / "corpus_offline")
    try:
        assert run(config) == 0
    finally:
        offline.rename(corpus)

    assert collection.count() == before
    assert "a.txt" in load_manifest(config)
    assert "not reachable" in capsys.readouterr().err


def test_pending_embedding_then_retry_once_dependencies_exist(tmp_path, monkeypatch, no_deps):
    corpus = write_corpus(tmp_path / "corpus", {"a.txt": body()})
    config = write_config(tmp_path, corpus)

    run(config)
    assert scrap(config, "a.txt")["status"] == ingest.PENDING_EMBEDDING

    collection = FakeCollection()
    monkeypatch.setattr(ingest, "chromadb", types.SimpleNamespace(), raising=False)
    monkeypatch.setattr(ingest.Embedder, "available", staticmethod(lambda: True))
    monkeypatch.setattr(ingest.Embedder, "encode",
                        lambda self, texts: _fake_vectors(texts, {"embedded": 0}))
    monkeypatch.setattr(ingest, "open_collection", lambda config: collection)

    run(config)

    assert collection.count() > 0
    assert scrap(config, "a.txt")["status"] == ingest.INDEXED


def test_pdf_without_text_layer_needs_ocr(tmp_path, store, monkeypatch):
    collection, _ = store
    corpus = write_corpus(tmp_path / "corpus", {"scan.pdf": "not really a pdf"})
    config = write_config(tmp_path, corpus)
    monkeypatch.setattr(ingest, "extract_pages",
                        lambda path, config: ([{"page": 1, "text": ""}], None))

    run(config)

    assert collection.count() == 0
    assert scrap(config, "scan.pdf")["status"] == ingest.NEEDS_OCR


def test_pdf_with_mixed_pages_is_indexed_and_reported(tmp_path, store, capsys, monkeypatch):
    collection, _ = store
    corpus = write_corpus(tmp_path / "corpus", {"mixed.pdf": "x"})
    config = write_config(tmp_path, corpus)
    monkeypatch.setattr(ingest, "extract_pages", lambda path, config: (
        [{"page": 1, "text": "Rule 1.1 Fees\n" + "text " * 60}, {"page": 2, "text": ""}], None))

    run(config)

    assert collection.count() > 0
    assert scrap(config, "mixed.pdf")["pages_without_text"] == 1
    assert "page(s) with no text" in capsys.readouterr().out


def test_zero_byte_file_is_empty(tmp_path, store):
    collection, _ = store
    corpus = write_corpus(tmp_path / "corpus", {"empty.txt": ""})
    config = write_config(tmp_path, corpus)

    run(config)

    assert collection.count() == 0
    assert scrap(config, "empty.txt")["status"] == ingest.EMPTY
    assert "zero-byte" in scrap(config, "empty.txt")["note"]


def test_unsupported_file_type_is_reported_not_fatal(tmp_path, store, capsys):
    collection, _ = store
    corpus = write_corpus(tmp_path / "corpus", {"old_order.doc": "binary-ish"})
    config = write_config(tmp_path, corpus, file_types=["doc"])

    assert run(config) == 0

    assert collection.count() == 0
    assert scrap(config, "old_order.doc")["status"] == ingest.UNSUPPORTED
    assert "Not indexed" in capsys.readouterr().out


def test_errors_are_parked_after_max_retries(tmp_path, store):
    collection, counters = store
    corpus = write_corpus(tmp_path / "corpus", {"a.txt": body()})
    config = write_config(tmp_path, corpus, max_retries=2)
    run(config)
    manifest = json.loads(Path(str(config.parent / "manifest.json")).read_text(encoding="utf-8"))

    entry = manifest["files"]["a.txt"]
    entry.update({"status": ingest.ERROR, "attempts": 2, "note": "boom"})
    Path(str(config.parent / "manifest.json")).write_text(json.dumps(manifest), encoding="utf-8")

    embedded_before = counters["embedded"]
    run(config)

    assert counters["embedded"] == embedded_before, "parked errors must not be retried"
    assert scrap(config, "a.txt")["status"] == ingest.ERROR


def test_strict_mode_exits_non_zero_for_blocked_files(tmp_path, store, capsys):
    corpus = write_corpus(tmp_path / "corpus", {"a.txt": body()})
    config = write_config(tmp_path, corpus)
    run(config)

    path = config.parent / "manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["files"]["a.txt"].update({"status": ingest.NEEDS_OCR, "attempts": 1})
    path.write_text(json.dumps(manifest), encoding="utf-8")

    assert run(config, "--strict") == 2
    assert "Strict mode" in capsys.readouterr().err


def test_manifest_from_a_newer_schema_is_refused(tmp_path, store, capsys):
    corpus = write_corpus(tmp_path / "corpus", {"a.txt": body()})
    config = write_config(tmp_path, corpus)
    (config.parent / "manifest.json").write_text(
        json.dumps({"schema_version": ingest.SCHEMA_VERSION + 1, "files": {}}), encoding="utf-8")

    assert run(config) == 1
    assert "schema_version" in capsys.readouterr().err


def test_broken_manifest_is_reported(tmp_path, store, capsys):
    corpus = write_corpus(tmp_path / "corpus", {"a.txt": body()})
    config = write_config(tmp_path, corpus)
    (config.parent / "manifest.json").write_text("{ not json", encoding="utf-8")

    assert run(config) == 1
    assert "Cannot read manifest" in capsys.readouterr().err


def test_rename_with_edit_is_flagged(tmp_path, store, capsys):
    # Same length, and the first/last 300 characters (what the dedup signature
    # looks at) are untouched, so only the hash can tell the documents apart.
    head, tail = "H" * 300, "T" * 300
    middle = "filler " * 100
    corpus = write_corpus(tmp_path / "corpus", {"a.txt": head + "\n" + middle + "\n" + tail})
    config = write_config(tmp_path, corpus)
    run(config)

    edited = head + "\n" + middle.replace("filler", "padded") + "\n" + tail
    (corpus / "a.txt").unlink()
    write_corpus(corpus, {"b.txt": edited})
    run(config)

    assert "content signature" in capsys.readouterr().out


def test_source_override_suppresses_stale_ignore_note(tmp_path, store, capsys):
    corpus = write_corpus(tmp_path / "corpus", {"a.txt": body()})
    (tmp_path / "ignore.json").write_text(json.dumps(["from_the_real_corpus.pdf"]),
                                          encoding="utf-8")
    config = write_config(tmp_path, corpus)

    run(config, "--source", str(corpus))

    assert "stale" not in capsys.readouterr().out


def test_duplicate_content_is_flagged(tmp_path, store, capsys):
    corpus = write_corpus(tmp_path / "corpus", {"one/a.txt": body(), "two/a.txt": body()})
    config = write_config(tmp_path, corpus)

    run(config)

    out = capsys.readouterr().out
    assert "content duplicate" in out
    assert "one/a.txt = two/a.txt" in out


def test_batch_progress_is_reported(tmp_path, store, capsys):
    collection, _ = store
    corpus = write_corpus(tmp_path / "corpus", {f"{name}.txt": body() for name in "abc"})
    config = write_config(tmp_path, corpus, batch_size=1)

    run(config)

    assert "changes processed" in capsys.readouterr().out
    assert set(load_manifest(config)) == {"a.txt", "b.txt", "c.txt"}
    assert collection.sources() == {"a.txt", "b.txt", "c.txt"}


def test_manifest_records_hash_status_and_page_metadata(tmp_path, store):
    corpus = write_corpus(tmp_path / "corpus", {"a.txt": body()})
    config = write_config(tmp_path, corpus)

    run(config)

    entry = scrap(config, "a.txt")
    assert len(entry["content_hash"]) == ingest.HASH_CHARS
    assert entry["status"] == ingest.INDEXED
    assert entry["page_count"] == 1
    assert entry["chunk_count"] > 0
    assert entry["attempts"] == 0
    assert entry["last_processed"].endswith("+00:00")
    assert entry["root"] == str(corpus)
    assert entry["embedding_fingerprint"] == ingest.embedding_fingerprint(
        yaml.safe_load(config.read_text(encoding="utf-8")))


def test_changing_the_embedding_model_reembeds_the_corpus(tmp_path, store):
    collection, counters = store
    corpus = write_corpus(tmp_path / "corpus", {"a.txt": body()})
    config = write_config(tmp_path, corpus)

    run(config)
    embedded = counters["embedded"]
    chunk_count = collection.count()

    update_config(config, embedding_model="other-model")
    run(config)

    entry = scrap(config, "a.txt")
    assert entry["status"] == ingest.INDEXED
    assert entry["embedding_fingerprint"] == ingest.embedding_fingerprint(
        yaml.safe_load(config.read_text(encoding="utf-8")))
    assert counters["embedded"] > embedded, "the chunks must be re-embedded"
    assert collection.count() == chunk_count, "chunk ids are stable, so no duplicates"

    embedded = counters["embedded"]
    run(config)                       # settled again: nothing more to re-embed
    assert counters["embedded"] == embedded


# --------------------------------------------------------------------------
# End-to-end OCR runs
# --------------------------------------------------------------------------

def test_ocr_pass_indexes_an_image_only_pdf(tmp_path, store, capsys, monkeypatch):
    collection, _ = store
    corpus = write_corpus(tmp_path / "corpus", {"scan.pdf": "x"})
    config = write_config(tmp_path, corpus, ocr_backend="hybrid")
    monkeypatch.setattr(ingest, "extract_pages",
                        lambda path, config: ([{"page": 1, "text": ""}], None))
    fake_pdf(monkeypatch)
    engine = FakeOcrEngine(text="Rule 9.1 Recovered. " * 20)
    monkeypatch.setattr(ingest, "build_ocr_engines", lambda config: [engine])

    assert run(config) == 0

    entry = scrap(config, "scan.pdf")
    assert entry["status"] == ingest.INDEXED
    assert entry["pages_without_text"] == 0
    assert entry["ocr_pages"] == 1
    assert entry["ocr_fingerprint"]
    assert collection.count() > 0
    out = capsys.readouterr().out
    assert "OCR: 1 page(s) OCR'd" in out
    assert "Searchable PDFs: skipped (ocrmypdf not installed)" in out
    assert engine.calls == 1


def test_ocr_recovers_blank_pages_in_an_already_indexed_pdf(tmp_path, store, monkeypatch):
    """The trap the design exists for: the content hash cannot change, so only the
    OCR fingerprint can make an already-indexed PDF with blank pages get re-processed."""
    collection, counters = store
    corpus = write_corpus(tmp_path / "corpus", {"mixed.pdf": "x"})
    config = write_config(tmp_path, corpus, ocr_backend="none")
    monkeypatch.setattr(ingest, "extract_pages", lambda path, config: (
        [{"page": 1, "text": "Rule 1.1 Fees\n" + "text " * 60}, {"page": 2, "text": ""}], None))

    run(config)                       # first pass: no OCR, page 2 has no text
    assert scrap(config, "mixed.pdf")["pages_without_text"] == 1
    assert "ocr_fingerprint" not in scrap(config, "mixed.pdf")
    embedded = counters["embedded"]

    update_config(config, ocr_backend="hybrid")
    fake_pdf(monkeypatch, page_count=2)
    engine = FakeOcrEngine(text="Recovered page two. " * 20)
    monkeypatch.setattr(ingest, "build_ocr_engines", lambda config: [engine])

    run(config)                       # content unchanged, so this is the fingerprint at work

    entry = scrap(config, "mixed.pdf")
    assert entry["pages_without_text"] == 0
    assert entry["ocr_pages"] == 1
    assert entry["ocr_fingerprint"]
    assert counters["embedded"] > embedded, "the file must be re-embedded once"

    embedded = counters["embedded"]
    run(config)                       # third run: settled, nothing to do
    assert counters["embedded"] == embedded


def test_audit_and_dry_run_never_call_the_ocr_engine(tmp_path, store, monkeypatch):
    corpus = write_corpus(tmp_path / "corpus", {"scan.pdf": "x"})
    config = write_config(tmp_path, corpus, ocr_backend="hybrid")
    monkeypatch.setattr(ingest, "extract_pages",
                        lambda path, config: ([{"page": 1, "text": ""}], None))
    fake_pdf(monkeypatch)
    engine = FakeOcrEngine()
    monkeypatch.setattr(ingest, "build_ocr_engines", lambda config: [engine])

    assert run(config, "--audit") == 0
    assert run(config, "--dry-run") == 0

    assert engine.calls == 0, "read-only modes must never spend money"
    assert not (tmp_path / "ocr_cache").exists()


def test_no_ocr_flag_disables_the_pass(tmp_path, store, monkeypatch):
    corpus = write_corpus(tmp_path / "corpus", {"scan.pdf": "x"})
    config = write_config(tmp_path, corpus, ocr_backend="hybrid")
    monkeypatch.setattr(ingest, "extract_pages",
                        lambda path, config: ([{"page": 1, "text": ""}], None))
    fake_pdf(monkeypatch)
    engine = FakeOcrEngine()
    monkeypatch.setattr(ingest, "build_ocr_engines", lambda config: [engine])

    run(config, "--no-ocr")

    assert engine.calls == 0
    assert scrap(config, "scan.pdf")["status"] == ingest.NEEDS_OCR


def test_ocr_limit_leaves_the_rest_for_the_next_run(tmp_path, store, monkeypatch):
    corpus = write_corpus(tmp_path / "corpus", {"scan.pdf": "x"})
    config = write_config(tmp_path, corpus, ocr_backend="hybrid")
    monkeypatch.setattr(ingest, "extract_pages", lambda path, config: (
        [{"page": 1, "text": ""}, {"page": 2, "text": ""}, {"page": 3, "text": ""}], None))
    fake_pdf(monkeypatch, page_count=3)
    engine = FakeOcrEngine(text="Recovered. " * 30)
    monkeypatch.setattr(ingest, "build_ocr_engines", lambda config: [engine])

    run(config, "--ocr-limit", "2")

    entry = scrap(config, "scan.pdf")
    assert engine.calls == 2
    assert entry["pages_without_text"] == 1
    assert "ocr_fingerprint" not in entry, "a capped file must stay un-settled"

    run(config)                       # page 1 and 2 come from cache, page 3 is OCR'd

    entry = scrap(config, "scan.pdf")
    assert entry["pages_without_text"] == 0
    assert entry["ocr_fingerprint"]
    assert engine.calls == 3


def test_searchable_pdf_artefact_is_written_for_ocrd_files_only(tmp_path, store, monkeypatch):
    corpus = write_corpus(tmp_path / "corpus", {"scan.pdf": "x", "plain.txt": body()})
    config = write_config(tmp_path, corpus, ocr_backend="hybrid")
    monkeypatch.setattr(ingest, "extract_pages", lambda path, config: (
        ([{"page": 1, "text": ""}] if path.suffix == ".pdf"
         else [{"page": None, "text": body()}]), None))
    fake_pdf(monkeypatch)
    engine = FakeOcrEngine(text="Recovered. " * 30)
    monkeypatch.setattr(ingest, "build_ocr_engines", lambda config: [engine])
    written = []

    def fake_ocr(source, destination, **kwargs):
        Path(destination).parent.mkdir(parents=True, exist_ok=True)
        Path(destination).write_bytes(b"%PDF-1.4 fake")
        written.append(destination)

    monkeypatch.setattr(ingest, "ocrmypdf", types.SimpleNamespace(ocr=fake_ocr))

    run(config)

    assert written == [str(tmp_path / "ocr_pdfs" / "scan.pdf")]
    assert (tmp_path / "ocr_pdfs" / "scan.pdf").exists()


def test_partial_ocr_recovery_is_indexed_with_a_note(tmp_path, store, monkeypatch):
    corpus = write_corpus(tmp_path / "corpus", {"partial.pdf": "x"})
    config = write_config(tmp_path, corpus, ocr_backend="hybrid")
    monkeypatch.setattr(ingest, "extract_pages", lambda path, config: (
        [{"page": 1, "text": ""}, {"page": 2, "text": ""}], None))
    fake_pdf(monkeypatch, page_count=2)
    engine = FakeOcrEngine(text="abcdefgh")      # recovered, but still far too short
    monkeypatch.setattr(ingest, "build_ocr_engines", lambda config: [engine])

    run(config)

    entry = scrap(config, "partial.pdf")
    assert entry["status"] == ingest.INDEXED
    assert entry["pages_without_text"] == 2
    assert entry["note"] == "low text layer: only partly recovered"


def test_ocr_engine_failure_becomes_an_error_and_is_retried(tmp_path, store, monkeypatch):
    corpus = write_corpus(tmp_path / "corpus", {"scan.pdf": "x"})
    config = write_config(tmp_path, corpus, ocr_backend="hybrid")
    monkeypatch.setattr(ingest, "extract_pages",
                        lambda path, config: ([{"page": 1, "text": ""}], None))
    fake_pdf(monkeypatch)
    engine = FakeOcrEngine(fail=True)
    monkeypatch.setattr(ingest, "build_ocr_engines", lambda config: [engine])

    run(config)

    entry = scrap(config, "scan.pdf")
    assert entry["status"] == ingest.ERROR
    assert "tesseract failed" in entry["note"]
    assert "ocr_fingerprint" not in entry, "a failed pass must stay retryable"
    assert engine.calls == 1

    run(config)                       # un-settled, so the next run tries again

    assert engine.calls == 2
    assert scrap(config, "scan.pdf")["attempts"] == 2


def test_searchable_pdf_is_written_when_the_text_came_from_cache(tmp_path, store, monkeypatch):
    corpus = write_corpus(tmp_path / "corpus", {"scan.pdf": "x"})
    config_path = write_config(tmp_path, corpus, ocr_backend="hybrid")
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    monkeypatch.setattr(ingest, "extract_pages",
                        lambda path, config: ([{"page": 1, "text": ""}], None))
    fake_pdf(monkeypatch)
    engine = FakeOcrEngine(text="")      # must not run: the cache already has the text
    monkeypatch.setattr(ingest, "build_ocr_engines", lambda config: [engine])

    # Leave the cache exactly as an interrupted earlier run would have.
    content_hash = ingest.hash_file(corpus / "scan.pdf")
    identifier = ingest.normalize_path(corpus / "scan.pdf")
    fingerprint = ingest.ocr_fingerprint(config, ["tesseract"])
    cache = ingest.OcrCache(config["ocr_cache_path"], identifier, content_hash, fingerprint)
    cache.set(1, "Recovered earlier. " * 20)
    cache.save()

    written = []

    def fake_ocr(source, destination, **kwargs):
        Path(destination).parent.mkdir(parents=True, exist_ok=True)
        Path(destination).write_bytes(b"%PDF-1.4 fake")
        written.append(destination)

    monkeypatch.setattr(ingest, "ocrmypdf", types.SimpleNamespace(ocr=fake_ocr))

    run(config_path)

    assert engine.calls == 0, "cached text must not be re-OCR'd"
    assert not scrap(config_path, "scan.pdf").get("ocr_pages")
    assert written == [str(tmp_path / "ocr_pdfs" / "scan.pdf")]
    assert (tmp_path / "ocr_pdfs" / "scan.pdf").exists()


def test_an_unknown_backend_warns_and_falls_back(tmp_path, store, capsys, monkeypatch):
    corpus = write_corpus(tmp_path / "corpus", {"a.txt": body()})
    config = write_config(tmp_path, corpus, ocr_backend="teseract")   # typo
    monkeypatch.setattr(ingest, "build_ocr_engines", lambda config: [])

    assert run(config) == 0

    assert "unknown ocr_backend" in capsys.readouterr().err


def test_an_unavailable_engine_is_reported_once(tmp_path, store, capsys, monkeypatch):
    corpus = write_corpus(tmp_path / "corpus", {"a.txt": body()})
    config = write_config(tmp_path, corpus, ocr_backend="hybrid")
    monkeypatch.setattr(ingest, "build_ocr_engines",
                        lambda config: [FakeOcrEngine(name="tesseract", available=False)])

    assert run(config) == 0

    assert "no engine is available" in capsys.readouterr().err
