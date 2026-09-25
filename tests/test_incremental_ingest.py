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
    }
    config.update(overrides)
    path = tmp_path / "config.yaml"
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
