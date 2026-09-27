"""
Tests for the RAG query CLI.

Everything is faked: the collection, the embedder, the LLM transport. No
sentence-transformers, ChromaDB, Ollama server or API key is needed.
"""

from __future__ import annotations

import io
import json
import sys
import types
import urllib.error
from pathlib import Path

import pytest
import yaml

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import incremental_ingest as ingest  # noqa: E402  (import after sys.path tweak)
import query_cli  # noqa: E402


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

class FakeCollection:
    """Stand-in for a ChromaDB collection; `rows` are (document, metadata, distance)."""

    def __init__(self, rows=None, count=None) -> None:
        self.rows = list(rows or [])
        self.calls: list[dict] = []
        self._count = count if count is not None else len(self.rows)

    def query(self, query_embeddings=None, n_results=None, include=None, where_document=None):
        self.calls.append({"n_results": n_results, "where_document": where_document,
                           "embeddings": query_embeddings})
        rows = self.rows
        if where_document:
            phrase = where_document.get("$contains", "")
            rows = [row for row in rows if phrase in (row[0] or "")]
        rows = rows[:n_results]
        return {"documents": [[row[0] for row in rows]],
                "metadatas": [[row[1] for row in rows]],
                "distances": [[row[2] for row in rows]]}

    def count(self) -> int:
        return self._count


class StubEmbedder:
    def __init__(self) -> None:
        self.queries: list[str] = []

    def encode(self, texts):
        self.queries.extend(texts)
        return [[0.1, 0.2] for _ in texts]


def row(document="text", metadata=None, distance=0.2):
    default = {"source": "a.pdf", "page": 1, "file_type": "pdf"}
    return (document, default if metadata is None else metadata, distance)


def make_hit(rank: int, page: int = 1, source: str = "a.pdf", section: str = "",
             file_type: str = "pdf", score: float = 0.8, text: str = "excerpt") -> "query_cli.Hit":
    return query_cli.Hit(rank=rank, text=text, source=source, page=page, section=section,
                         file_type=file_type, score=score)


class StubRetriever:
    def __init__(self, hits) -> None:
        self.hits = list(hits)
        self.calls: list[dict] = []

    def search(self, question, top_k=None, min_score=None, contains=None):
        self.calls.append({"question": question, "top_k": top_k,
                           "min_score": min_score, "contains": contains})
        return list(self.hits)


class FakeLlm:
    name = "fake"

    def __init__(self, text: str = "Answer [1]", fail: bool = False, available: bool = True) -> None:
        self.text = text
        self.fail = fail
        self._available = available
        self.system: str | None = None
        self.user: str | None = None

    def available(self) -> bool:
        return self._available

    def complete(self, system: str, user: str) -> str:
        self.system, self.user = system, user
        if self.fail:
            raise query_cli.LlmError("engine exploded")
        return self.text


class FakeResponse:
    def __init__(self, body: bytes) -> None:
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def read(self) -> bytes:
        return self.body


def write_config(tmp_path: Path, **overrides) -> Path:
    config = {
        "docs_source": [str(tmp_path / "corpus")],
        "file_types": ["pdf", "txt"],
        "ignore_list_path": str(tmp_path / "ignore.json"),
        "manifest_path": str(tmp_path / "manifest.json"),
        "chroma_path": str(tmp_path / "chroma"),
        "collection_name": "test_collection",
        "embedding_model": "fake-model",
        "embedding_batch_size": 8,
        "llm_backend": "none",
        "llm_top_k": 6,
        "llm_min_score": 0.0,
        "llm_max_context_chars": 6000,
    }
    config.update(overrides)
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(config), encoding="utf-8")
    return path


# --------------------------------------------------------------------------
# Citations
# --------------------------------------------------------------------------

def test_citation_uses_the_page_number():
    assert make_hit(1, page=12, source="order.pdf").citation() == "order.pdf, p.12"


def test_citation_uses_the_sheet_name_for_spreadsheets():
    hit = make_hit(1, page=-1, source="tariff.xlsx", section="Tariff 2024", file_type="xlsx")
    assert hit.citation() == 'tariff.xlsx, sheet "Tariff 2024"'


def test_citation_uses_the_heading_for_page_less_documents():
    hit = make_hit(1, page=-1, source="rules.docx", section="Rule 4 Fees", file_type="docx")
    assert hit.citation() == "rules.docx, Rule 4 Fees"


def test_citation_falls_back_to_the_file():
    assert make_hit(1, page=-1, source="notes.txt", file_type="txt").citation() == "notes.txt"


# --------------------------------------------------------------------------
# Retrieval
# --------------------------------------------------------------------------

def test_search_maps_cosine_distance_to_a_score_and_orders_hits():
    collection = FakeCollection([row("first", {"source": "a.pdf", "page": 1}, 0.2),
                                 row("second", {"source": "b.pdf", "page": 9}, 0.5)])
    retriever = query_cli.Retriever({}, collection, StubEmbedder())

    hits = retriever.search("fees", top_k=2)

    assert [hit.text for hit in hits] == ["first", "second"]
    assert hits[0].score == pytest.approx(0.8)
    assert hits[1].score == pytest.approx(0.5)
    assert [hit.rank for hit in hits] == [1, 2]
    assert hits[1].citation() == "b.pdf, p.9"


def test_search_requests_a_candidate_pool_and_applies_contains():
    collection = FakeCollection([row("Rule 14(3) text", {"source": "a.pdf", "page": 2}, 0.3),
                                 row("unrelated", {"source": "b.pdf", "page": 3}, 0.4)])
    embedder = StubEmbedder()
    retriever = query_cli.Retriever({}, collection, embedder)

    hits = retriever.search("late fee", top_k=5, contains="Rule 14(3)")

    assert collection.calls[0]["n_results"] == 20, "a wider pool than top_k is requested"
    assert collection.calls[0]["where_document"] == {"$contains": "Rule 14(3)"}
    assert embedder.queries == ["late fee"]
    assert [hit.text for hit in hits] == ["Rule 14(3) text"]
    assert hits[0].rank == 1, "ranks must stay contiguous after filtering"


def test_search_applies_min_score_and_top_k():
    collection = FakeCollection([row(f"h{n}", {"source": "a.pdf", "page": n}, distance)
                                 for n, distance in enumerate([0.1, 0.4, 0.9], start=1)])
    retriever = query_cli.Retriever({}, collection, StubEmbedder())

    hits = retriever.search("q", top_k=3, min_score=0.5)

    assert [hit.text for hit in hits] == ["h1", "h2"], "the 0.1-score hit is dropped"

    hits = retriever.search("q", top_k=1)
    assert len(hits) == 1 and hits[0].rank == 1


def test_search_does_not_query_without_a_question():
    collection = FakeCollection([row()])
    retriever = query_cli.Retriever({}, collection, StubEmbedder())

    assert retriever.search("   ") == []
    assert collection.calls == []


def test_search_tolerates_missing_metadata():
    collection = FakeCollection([row("bare", {}, 0.0)])
    retriever = query_cli.Retriever({}, collection, StubEmbedder())

    hits = retriever.search("q")

    assert hits[0].page == -1
    assert hits[0].section == ""
    assert hits[0].citation() == ""


# --------------------------------------------------------------------------
# Prompt / context
# --------------------------------------------------------------------------

def test_build_context_numbers_excerpts():
    hits = [make_hit(1, text="alpha"), make_hit(2, page=4, source="b.pdf", text="beta")]

    context, truncated = query_cli.build_context(hits)

    assert "[1] a.pdf, p.1" in context
    assert "[2] b.pdf, p.4" in context
    assert truncated is False


def test_build_context_caps_the_total_size():
    hits = [make_hit(1, text="x" * 400), make_hit(2, text="y" * 400)]

    context, truncated = query_cli.build_context(hits, max_chars=300)

    assert truncated is True
    assert len(context) == 300
    assert "[2]" not in context, "the second excerpt is dropped once the cap is reached"


def test_build_context_clips_a_single_oversized_excerpt():
    context, truncated = query_cli.build_context([make_hit(1, text="z" * 1000)], max_chars=100)

    assert truncated is True
    assert len(context) == 100


def test_build_user_prompt_contains_excerpts_and_question():
    prompt = query_cli.build_user_prompt("what is the fee?", "[1] a.pdf, p.1\ntext")
    assert "Excerpts:" in prompt
    assert "what is the fee?" in prompt


# --------------------------------------------------------------------------
# Citation validation + extractive answer
# --------------------------------------------------------------------------

def test_validate_citations_flags_unknown_and_missing_markers():
    hits = [make_hit(1), make_hit(2)]

    cited, warnings = query_cli.validate_citations("See [1] and [7].", hits)
    assert cited == [1]
    assert any("[7]" in warning for warning in warnings)

    _, warnings = query_cli.validate_citations("An answer without markers.", hits)
    assert any("no [n] citations" in warning for warning in warnings)

    cited, warnings = query_cli.validate_citations("", hits)
    assert cited == [] and warnings == []


def test_extractive_answer_is_cited_and_truncated():
    answer = query_cli.extractive_answer([make_hit(1, page=7, source="a.pdf", text="word " * 300)])

    assert answer.startswith("[1] a.pdf, p.7")
    assert "…" in answer


# --------------------------------------------------------------------------
# LLM providers
# --------------------------------------------------------------------------

def test_ollama_posts_chat_and_reads_the_reply(monkeypatch):
    captured: dict = {}

    def fake_urlopen(request, timeout=None):
        captured["url"] = request.full_url
        captured["body"] = json.loads(request.data.decode("utf-8"))
        captured["timeout"] = timeout
        return FakeResponse(json.dumps({"message": {"content": "Answer [1]"}}).encode())

    monkeypatch.setattr(query_cli.urllib.request, "urlopen", fake_urlopen)
    llm = query_cli.OllamaLlm({"llm_ollama_url": "http://localhost:11434/",
                               "llm_ollama_model": "llama3.2:3b", "llm_ollama_timeout": 9})

    assert llm.available() is True
    assert llm.complete("SYS", "USER") == "Answer [1]"
    assert captured["url"] == "http://localhost:11434/api/chat"
    assert captured["timeout"] == 9
    assert captured["body"]["stream"] is False
    assert captured["body"]["model"] == "llama3.2:3b"
    assert captured["body"]["messages"][0] == {"role": "system", "content": "SYS"}
    assert captured["body"]["messages"][1]["content"] == "USER"


def test_claude_posts_messages_with_a_key_from_the_environment(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "secret")
    captured: dict = {}

    def fake_urlopen(request, timeout=None):
        captured["url"] = request.full_url
        captured["headers"] = request.headers
        captured["body"] = json.loads(request.data.decode("utf-8"))
        return FakeResponse(json.dumps(
            {"content": [{"type": "text", "text": "Answer"}, {"type": "text", "text": " [2]"}]}
        ).encode())

    monkeypatch.setattr(query_cli.urllib.request, "urlopen", fake_urlopen)
    llm = query_cli.ClaudeLlm({"llm_claude_model": "claude-sonnet-4-5"})

    assert llm.available() is True
    assert llm.complete("SYS", "USER") == "Answer [2]"
    assert captured["url"] == "https://api.anthropic.com/v1/messages"
    assert captured["headers"]["X-api-key"] == "secret"
    assert captured["headers"]["Anthropic-version"] == "2023-06-01"
    assert captured["body"]["system"] == "SYS"
    assert captured["body"]["max_tokens"] == 1024


def test_claude_is_unavailable_without_a_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert query_cli.ClaudeLlm({}).available() is False
    monkeypatch.setenv("ANTHROPIC_API_KEY", "secret")
    assert query_cli.ClaudeLlm({"llm_claude_api_key_env": "OTHER_KEY"}).available() is False


def test_post_json_wraps_transport_errors(monkeypatch):
    def http_error(request, timeout=None):
        raise urllib.error.HTTPError(request.full_url, 401, "Unauthorized", {},
                                     io.BytesIO(b"bad key"))

    monkeypatch.setattr(query_cli.urllib.request, "urlopen", http_error)
    with pytest.raises(query_cli.LlmError) as exc:
        query_cli.post_json("http://x", {}, {}, 5)
    assert "401" in str(exc.value)

    def url_error(request, timeout=None):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(query_cli.urllib.request, "urlopen", url_error)
    with pytest.raises(query_cli.LlmError) as exc:
        query_cli.post_json("http://x", {}, {}, 5)
    assert "cannot reach" in str(exc.value)

    monkeypatch.setattr(query_cli.urllib.request, "urlopen",
                        lambda request, timeout=None: FakeResponse(b"not json"))
    with pytest.raises(query_cli.LlmError) as exc:
        query_cli.post_json("http://x", {}, {}, 5)
    assert "malformed JSON" in str(exc.value)


def test_response_parsers_handle_alternate_shapes():
    assert query_cli.extract_ollama_text({"response": "generate-style"}) == "generate-style"
    assert query_cli.extract_ollama_text({}) == ""
    assert query_cli.extract_ollama_text("nope") == ""
    assert query_cli.extract_claude_text({"content": "plain"}) == "plain"
    assert query_cli.extract_claude_text({}) == ""


def test_select_llm_maps_backends_and_warns_on_a_typo(capsys):
    assert isinstance(query_cli.select_llm({"llm_backend": "ollama"}), query_cli.OllamaLlm)
    assert isinstance(query_cli.select_llm({"llm_backend": "claude"}), query_cli.ClaudeLlm)
    assert query_cli.select_llm({"llm_backend": "none"}) is None
    assert query_cli.select_llm({}, override="none") is None

    assert isinstance(query_cli.select_llm({"llm_backend": "claud"}), query_cli.OllamaLlm)
    assert "unknown llm_backend" in capsys.readouterr().err


# --------------------------------------------------------------------------
# run_query
# --------------------------------------------------------------------------

def test_run_query_uses_the_llm_and_validates_citations():
    llm = FakeLlm(text="Fees are X [1] and also [9].")
    retriever = StubRetriever([make_hit(1), make_hit(2)])

    result = query_cli.run_query("fees?", retriever, llm)

    assert result.mode == "llm"
    assert result.cited == [1]
    assert any("[9]" in warning for warning in result.warnings)
    assert "[1] a.pdf, p.1" in llm.user and "fees?" in llm.user


def test_run_query_falls_back_to_excerpts_when_the_llm_errors():
    result = query_cli.run_query("fees?", StubRetriever([make_hit(1)]), FakeLlm(fail=True))

    assert result.mode == "extractive"
    assert result.answer.startswith("[1] a.pdf, p.1")
    assert any("engine exploded" in warning for warning in result.warnings)


def test_run_query_is_extractive_without_an_llm():
    result = query_cli.run_query("fees?", StubRetriever([make_hit(1)]), None)

    assert result.mode == "extractive"
    assert result.error is None


def test_run_query_degrades_when_claude_has_no_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    result = query_cli.run_query("fees?", StubRetriever([make_hit(1)]), query_cli.ClaudeLlm({}))

    assert result.mode == "extractive"
    assert any("not configured" in warning for warning in result.warnings)


def test_run_query_without_hits():
    result = query_cli.run_query("fees?", StubRetriever([]), FakeLlm())

    assert result.mode == "none"
    assert result.hits == []
    assert result.warnings


def test_run_query_warns_when_the_context_is_truncated():
    hits = [make_hit(1, text="x" * 500), make_hit(2, text="y" * 500)]

    result = query_cli.run_query("q", StubRetriever(hits), FakeLlm(), max_context_chars=300)

    assert any("truncated" in warning for warning in result.warnings)


def test_run_query_reports_an_empty_model_answer():
    result = query_cli.run_query("q", StubRetriever([make_hit(1)]), FakeLlm(text="   "))

    assert result.mode == "extractive"
    assert any("empty answer" in warning for warning in result.warnings)


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------

def test_render_result_lists_sources_and_marks_cited():
    result = query_cli.QueryResult(question="q", answer="Answer [1]", mode="llm",
                                   hits=[make_hit(1, page=3, score=0.812), make_hit(2, page=4)],
                                   cited=[1])

    rendered = query_cli.render_result(result)

    assert "a.pdf, p.3" in rendered
    assert "(score 0.812)" in rendered
    assert "[cited]" in rendered


def test_query_result_to_dict_is_json_safe():
    result = query_cli.QueryResult(question="q", answer="a", mode="llm",
                                   hits=[make_hit(1, score=0.8)], cited=[1], warnings=["w"])

    payload = json.loads(json.dumps(result.to_dict()))

    assert payload["question"] == "q"
    assert payload["sources"][0]["citation"] == "a.pdf, p.1"
    assert payload["sources"][0]["page"] == 1


# --------------------------------------------------------------------------
# CLI plumbing
# --------------------------------------------------------------------------

def test_open_retriever_errors_without_the_indexing_stack(monkeypatch, capsys):
    monkeypatch.setattr(ingest, "chromadb", None)
    assert query_cli.open_retriever({}) is None
    assert "sentence-transformers" in capsys.readouterr().err


def test_open_retriever_errors_on_an_empty_collection(monkeypatch, capsys):
    monkeypatch.setattr(ingest, "chromadb", types.SimpleNamespace())
    monkeypatch.setattr(ingest.Embedder, "available", staticmethod(lambda: True))
    monkeypatch.setattr(ingest, "open_collection", lambda config: FakeCollection(count=0))

    assert query_cli.open_retriever({"collection_name": "x"}) is None
    assert "empty" in capsys.readouterr().err


def install_fake_stack(monkeypatch, collection) -> None:
    """Wire the CLI's ingest dependency to fakes, as the incremental tests do."""
    monkeypatch.setattr(ingest, "chromadb", types.SimpleNamespace())
    monkeypatch.setattr(ingest.Embedder, "available", staticmethod(lambda: True))
    monkeypatch.setattr(ingest.Embedder, "encode",
                        lambda self, texts: [[0.1, 0.2] for _ in texts])
    monkeypatch.setattr(ingest, "open_collection", lambda config: collection)


def test_main_end_to_end_returns_cited_excerpts(tmp_path, monkeypatch, capsys):
    collection = FakeCollection([row("Rule 14(3): fee is 2%.", 
                                     {"source": "order.pdf", "page": 12, "file_type": "pdf"}, 0.1)])
    install_fake_stack(monkeypatch, collection)
    config = write_config(tmp_path)

    code = query_cli.main(["--config", str(config), "what is the fee?", "--no-llm"])

    out = capsys.readouterr().out
    assert code == 0
    assert "order.pdf, p.12" in out
    assert "Sources:" in out


def test_main_json_output(tmp_path, monkeypatch, capsys):
    collection = FakeCollection([row("text", {"source": "order.pdf", "page": 3,
                                             "file_type": "pdf"}, 0.1)])
    install_fake_stack(monkeypatch, collection)
    config = write_config(tmp_path)

    code = query_cli.main(["--config", str(config), "q", "--no-llm", "--json"])

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["mode"] == "extractive"
    assert payload["sources"][0]["citation"] == "order.pdf, p.3"


def test_main_returns_2_when_nothing_matches(tmp_path, monkeypatch, capsys):
    collection = FakeCollection(rows=[], count=3)      # indexed, but the query finds nothing
    install_fake_stack(monkeypatch, collection)
    config = write_config(tmp_path)

    assert query_cli.main(["--config", str(config), "q", "--no-llm"]) == 2
    assert "No relevant excerpts" in capsys.readouterr().out


def test_main_requires_a_question(tmp_path, monkeypatch):
    install_fake_stack(monkeypatch, FakeCollection([row()]))
    config = write_config(tmp_path)

    assert query_cli.main(["--config", str(config)]) == 1


def test_main_setup_error_when_deps_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(ingest, "chromadb", None)
    config = write_config(tmp_path)

    assert query_cli.main(["--config", str(config), "q"]) == 1


def test_repl_exits_cleanly_on_eof(monkeypatch, capsys):
    def eof(_prompt=""):
        raise EOFError

    monkeypatch.setattr("builtins.input", eof)
    args = query_cli.parse_args(["-i"])

    assert query_cli.repl(args, {}, StubRetriever([]), None) == 0
    assert "Interactive KERC query" in capsys.readouterr().out


def test_parse_args_defaults():
    args = query_cli.parse_args(["a question"])
    assert args.question == "a question"
    assert args.top_k is None
    assert args.llm is None
    assert args.no_llm is False
