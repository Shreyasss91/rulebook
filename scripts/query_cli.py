#!/usr/bin/env python3
"""
Query the indexed KERC corpus and answer with page-exact citations.

Reads the ChromaDB collection that `scripts/incremental_ingest.py` wrote (same
config keys: `chroma_path`, `collection_name`, `embedding_model`), embeds the
question, retrieves the best-matching chunks, and either asks a configured LLM
for an answer or returns the cited excerpts directly.

Every claim is traceable to a file and page: the excerpts handed to the model are
numbered `[1]…[k]`, the model is told to cite with those markers, and the markers
are validated afterwards. The retrieved sources are printed with their scores so
an answer can be checked even when the model misbehaves.

    question -> embed -> vector search -> numbered excerpts -> LLM -> answer + [n]
                                                          \\-> extractive fallback

Usage:
    python scripts/query_cli.py "what is the late payment surcharge?"
    python scripts/query_cli.py -i
    python scripts/query_cli.py "..." --show-context
    python scripts/query_cli.py "..." --json
    python scripts/query_cli.py "..." --no-llm
    python scripts/query_cli.py "Rule 14(3)" --contains "Rule 14(3)"

Exit codes: 0 answered, 1 setup error (dependencies or empty/missing index),
2 no relevant excerpts found.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

# The ingest script owns the config loading and the vector-store handle, so the
# query side reuses them rather than keeping a second copy of the same rules.
import incremental_ingest as ingest

DEFAULT_TOP_K = 6
DEFAULT_LLM_BACKEND = "ollama"
# A question is embedded once; retrieving a wider pool than `top_k` and then
# filtering keeps `--min-score`/`--top-k` from starving the result set.
CANDIDATE_MULTIPLIER = 4
MAX_CANDIDATES = 100

SYSTEM_PROMPT = (
    "You answer questions about KERC (Karnataka Electricity Regulatory Commission) "
    "regulatory documents using ONLY the numbered excerpts supplied by the user.\n"
    "Rules:\n"
    "1. Base every statement on the excerpts; never use outside knowledge.\n"
    "2. Cite the excerpts you rely on inline with bracketed numbers, e.g. [1] or [2][3].\n"
    "3. Quote rule, section, clause and tariff numbers exactly as they appear.\n"
    "4. If the excerpts do not contain the answer, say so plainly instead of guessing.\n"
    "5. Answer concisely, in prose, in the language of the question."
)

# `[1]`, `[12]` … the only citation form the prompt allows.
CITATION_RE = re.compile(r"\[(\d{1,3})\]")
PAGE_LESS_TYPES = {"xlsx", "xls", "xlsm", "csv"}


class RetrievalError(Exception):
    """The collection or the embedding model is not usable."""


class LlmError(Exception):
    """The configured answer engine could not be reached or understood."""


# --------------------------------------------------------------------------
# Retrieval
# --------------------------------------------------------------------------

@dataclass
class Hit:
    """One retrieved chunk, with everything needed to cite it."""

    rank: int
    text: str
    source: str
    page: int
    section: str
    file_type: str
    score: float
    chunk: int = 0
    chunk_total: int = 0
    root: str = ""

    def citation(self) -> str:
        """
        The page-exact reference for this chunk.

        `page > 0` is the normal case (PDF/OCR); `page == -1` means the format has
        no pages, so the sheet/heading `section` stands in, and a plain
        whole-document label is the last resort.
        """
        if self.page and self.page > 0:
            return f"{self.source}, p.{self.page}"
        if self.section:
            if self.file_type in PAGE_LESS_TYPES:
                return f'{self.source}, sheet "{self.section}"'
            return f"{self.source}, {self.section}"
        return self.source

    def to_dict(self) -> dict:
        return {
            "rank": self.rank,
            "citation": self.citation(),
            "source": self.source,
            "page": self.page,
            "section": self.section,
            "file_type": self.file_type,
            "score": round(self.score, 6),
        }


def _first(value) -> list:
    """Chroma returns a list per query; the CLI always sends exactly one."""
    if not value:
        return []
    first = value[0]
    return first if isinstance(first, list) else []


def _as_int(value, default: int = -1) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def hit_from_row(document, metadata: dict, distance, rank: int) -> Hit:
    metadata = metadata or {}
    return Hit(
        rank=rank,
        text=document or "",
        source=str(metadata.get("source", "")),
        page=_as_int(metadata.get("page", -1)),
        section=str(metadata.get("section") or ""),
        file_type=str(metadata.get("file_type") or ""),
        score=1.0 - float(distance),          # the collection is cosine space
        chunk=_as_int(metadata.get("chunk", 0), 0),
        chunk_total=_as_int(metadata.get("chunk_total", 0), 0),
        root=str(metadata.get("root") or ""),
    )


class Retriever:
    """Vector search over the ingested collection, with page-exact metadata."""

    def __init__(self, config: dict, collection, embedder=None) -> None:
        self.config = config
        self.collection = collection
        self.embedder = embedder if embedder is not None else ingest.Embedder(config)
        self.top_k = int(config.get("llm_top_k", DEFAULT_TOP_K) or DEFAULT_TOP_K)
        self.min_score = float(config.get("llm_min_score", 0.0) or 0.0)

    def search(self, question: str, top_k: int | None = None, min_score: float | None = None,
               contains: str | None = None) -> list[Hit]:
        if not question or not question.strip():
            return []
        if self.collection is None:
            raise RetrievalError("no vector store is open")
        top_k = int(top_k) if top_k else self.top_k
        floor = self.min_score if min_score is None else float(min_score)
        pool = min(max(top_k * CANDIDATE_MULTIPLIER, top_k), MAX_CANDIDATES)

        query_args = {
            "query_embeddings": self.embedder.encode([question]),
            "n_results": pool,
            "include": ["documents", "metadatas", "distances"],
        }
        # Exact substring lookup (Chroma's $contains) for rule numbers and quoted
        # phrases the embedding might rank poorly.
        if contains:
            query_args["where_document"] = {"$contains": contains}

        result = self.collection.query(**query_args)
        candidates = zip(_first(result.get("documents")), _first(result.get("metadatas")),
                         _first(result.get("distances")))

        hits: list[Hit] = []
        for document, metadata, distance in candidates:
            hit = hit_from_row(document, metadata, distance, rank=len(hits) + 1)
            if hit.score < floor:
                continue
            hits.append(hit)
            if len(hits) >= top_k:
                break
        return hits


# --------------------------------------------------------------------------
# Prompt construction
# --------------------------------------------------------------------------

def build_context(hits: list[Hit], max_chars: int | None = None) -> tuple[str, bool]:
    """
    Numbered excerpt blocks for the prompt. Returns (context, truncated).

    `max_chars` caps the total so a 500-page tariff order cannot blow the model's
    context window; one oversized excerpt is still clipped rather than dropped, so
    the question never arrives with an empty context.
    """
    blocks: list[str] = []
    used = 0
    truncated = False
    for hit in hits:
        block = f"[{hit.rank}] {hit.citation()}\n{hit.text.strip()}"
        if max_chars is not None and used and used + len(block) > max_chars:
            truncated = True
            break
        if max_chars is not None and len(block) > max_chars:
            block = block[:max_chars]
            truncated = True
        blocks.append(block)
        used += len(block) + 2
    return "\n\n".join(blocks), truncated


def build_user_prompt(question: str, context: str) -> str:
    return f"Excerpts:\n\n{context}\n\nQuestion: {question}"


# --------------------------------------------------------------------------
# Answer engines
# --------------------------------------------------------------------------

def post_json(url: str, payload: dict, headers: dict, timeout: float) -> dict:
    """POST a JSON body and parse the JSON reply, turning transport errors into LlmError."""
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", **headers},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "ignore")[:300]
        except Exception:  # pragma: no cover - best effort
            pass
        raise LlmError(f"HTTP {exc.code} from {url}: {detail or exc.reason}") from exc
    except (urllib.error.URLError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        raise LlmError(f"cannot reach {url}: {reason}") from exc
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise LlmError(f"malformed JSON from {url}: {exc}") from exc


def extract_ollama_text(body) -> str:
    """Ollama /api/chat returns message.content; /api/generate returns response."""
    if not isinstance(body, dict):
        return ""
    message = body.get("message")
    if isinstance(message, dict) and isinstance(message.get("content"), str):
        return message["content"]
    response = body.get("response")
    return response if isinstance(response, str) else ""


def extract_claude_text(body) -> str:
    """Anthropic /v1/messages returns content as a list of text parts."""
    if not isinstance(body, dict):
        return ""
    content = body.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(part.get("text", "") for part in content if isinstance(part, dict))
    return ""


class Llm:
    """One answer engine. `available()` is cheap; the request happens on `complete`."""

    name = "none"

    def available(self) -> bool:
        return False

    def complete(self, system: str, user: str) -> str:
        raise NotImplementedError


class OllamaLlm(Llm):
    """Local Ollama server — free, private, the default (docs/origin_doc.md)."""

    name = "ollama"

    def __init__(self, config: dict) -> None:
        self.url = str(config.get("llm_ollama_url", "http://localhost:11434")).rstrip("/")
        self.model = str(config.get("llm_ollama_model", "llama3.2:3b"))
        self.timeout = config.get("llm_ollama_timeout", 120)

    def available(self) -> bool:
        # A configured model is assumed reachable; the server is contacted on use
        # and a connection failure degrades to the extractive answer.
        return bool(self.model)

    def complete(self, system: str, user: str) -> str:
        payload = {
            "model": self.model,
            "stream": False,
            "options": {"temperature": 0},
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        body = post_json(f"{self.url}/api/chat", payload, {}, self.timeout)
        return extract_ollama_text(body)


class ClaudeLlm(Llm):
    """Anthropic Messages API — better reasoning, needs ANTHROPIC_API_KEY."""

    name = "claude"

    def __init__(self, config: dict) -> None:
        self.url = str(config.get("llm_claude_url", "https://api.anthropic.com/v1/messages"))
        self.model = str(config.get("llm_claude_model", "claude-sonnet-4-5"))
        self.key_env = str(config.get("llm_claude_api_key_env", "ANTHROPIC_API_KEY"))
        self.max_tokens = int(config.get("llm_claude_max_tokens", 1024) or 1024)
        self.timeout = config.get("llm_claude_timeout", 120)
        self.version = str(config.get("llm_anthropic_version", "2023-06-01"))

    def api_key(self) -> str:
        return os.environ.get(self.key_env, "").strip()

    def available(self) -> bool:
        return bool(self.api_key()) and bool(self.model)

    def complete(self, system: str, user: str) -> str:
        payload = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "temperature": 0,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        }
        headers = {"x-api-key": self.api_key(), "anthropic-version": self.version}
        body = post_json(self.url, payload, headers, self.timeout)
        return extract_claude_text(body)


def select_llm(config: dict, override: str | None = None) -> Llm | None:
    """Map `llm_backend` (or a `--llm` override) to an engine; None means retrieval-only."""
    backend = str(override or config.get("llm_backend", DEFAULT_LLM_BACKEND)).strip().lower()
    if backend in ("", "off", "false", "no", "disabled", "none"):
        return None
    if backend in ("claude", "anthropic"):
        return ClaudeLlm(config)
    if backend in ("ollama", "local"):
        return OllamaLlm(config)
    # The wrong guess could ship documents to a cloud API, so warn and stay local.
    print(f"Warning: unknown llm_backend '{backend}'; using '{DEFAULT_LLM_BACKEND}'", file=sys.stderr)
    return OllamaLlm(config)


# --------------------------------------------------------------------------
# Answering
# --------------------------------------------------------------------------

def validate_citations(answer: str, hits: list[Hit]) -> tuple[list[int], list[str]]:
    """
    Check the answer's `[n]` markers against the excerpts that were supplied.

    Returns (cited ranks that exist, warnings). A marker outside the supplied range
    is a hallucinated citation and is surfaced rather than printed as if valid.
    """
    available = {hit.rank for hit in hits}
    used = sorted({int(match) for match in CITATION_RE.findall(answer or "")})
    warnings: list[str] = []
    unknown = [number for number in used if number not in available]
    if unknown:
        warnings.append("answer cites excerpt(s) that were not provided: "
                        + ", ".join(f"[{number}]" for number in unknown))
    if not used and (answer or "").strip():
        warnings.append("answer contains no [n] citations")
    return [number for number in used if number in available], warnings


def extractive_answer(hits: list[Hit], max_chars: int = 600) -> str:
    """The no-LLM answer: the retrieved excerpts, cited, with no synthesis."""
    parts: list[str] = []
    for hit in hits:
        snippet = " ".join(hit.text.split())
        if len(snippet) > max_chars:
            snippet = snippet[:max_chars].rstrip() + "…"
        parts.append(f"[{hit.rank}] {hit.citation()}\n{snippet}")
    return "\n\n".join(parts)


@dataclass
class QueryResult:
    question: str
    answer: str
    mode: str                       # llm | extractive | none
    hits: list[Hit] = field(default_factory=list)
    cited: list[int] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    error: str | None = None

    def to_dict(self) -> dict:
        return {
            "question": self.question,
            "answer": self.answer,
            "mode": self.mode,
            "cited": self.cited,
            "warnings": self.warnings,
            "error": self.error,
            "sources": [hit.to_dict() for hit in self.hits],
        }


def run_query(question: str, retriever: Retriever, llm: Llm | None,
              top_k: int | None = None, min_score: float | None = None,
              contains: str | None = None,
              max_context_chars: int | None = None) -> QueryResult:
    """Retrieve, then answer with the LLM or fall back to the cited excerpts."""
    hits = retriever.search(question, top_k=top_k, min_score=min_score, contains=contains)
    if not hits:
        return QueryResult(question=question, answer="", mode="none",
                           warnings=["no relevant excerpts were found"])

    context, truncated = build_context(hits, max_context_chars)
    warnings: list[str] = []
    if truncated:
        warnings.append("excerpts were truncated to fit llm_max_context_chars")

    if llm is None or not llm.available():
        reason = None if llm is None else f"{llm.name} is not configured (key/model missing)"
        if reason:
            warnings.append(reason)
        return QueryResult(question=question, answer=extractive_answer(hits),
                           mode="extractive", hits=hits, warnings=warnings, error=reason)

    try:
        answer = (llm.complete(SYSTEM_PROMPT, build_user_prompt(question, context)) or "").strip()
    except LlmError as exc:
        warnings.append(f"{llm.name} unavailable: {exc}")
        return QueryResult(question=question, answer=extractive_answer(hits),
                           mode="extractive", hits=hits, warnings=warnings, error=str(exc))

    if not answer:
        warnings.append(f"{llm.name} returned an empty answer")
        return QueryResult(question=question, answer=extractive_answer(hits),
                           mode="extractive", hits=hits, warnings=warnings)

    cited, citation_warnings = validate_citations(answer, hits)
    warnings.extend(citation_warnings)
    return QueryResult(question=question, answer=answer, mode="llm",
                       hits=hits, cited=cited, warnings=warnings)


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------

def render_result(result: QueryResult, show_context: bool = False) -> str:
    if result.mode == "none":
        return "No relevant excerpts were found in the collection."
    lines = [result.answer, "", "Sources:"]
    for hit in result.hits:
        cited = "  [cited]" if hit.rank in result.cited else ""
        lines.append(f"  [{hit.rank}] {hit.citation()}  (score {hit.score:.3f}){cited}")
        if show_context:
            for line in hit.text.strip().splitlines():
                lines.append(f"        {line}")
    return "\n".join(lines)


def emit(result: QueryResult, args) -> int:
    if args.json:
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    else:
        print(render_result(result, show_context=args.show_context))
    for warning in result.warnings:
        print(f"Warning: {warning}", file=sys.stderr)
    return 2 if result.mode == "none" else 0


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Query the indexed KERC corpus and answer with page-exact citations.")
    parser.add_argument("question", nargs="?",
                        help="the question; omit it with --interactive")
    parser.add_argument("--config", default="config.yaml", help="config file (default: config.yaml)")
    parser.add_argument("-i", "--interactive", action="store_true",
                        help="read questions in a loop until 'exit'")
    parser.add_argument("-k", "--top-k", type=int, default=None,
                        help="excerpts to retrieve (default: llm_top_k)")
    parser.add_argument("--min-score", type=float, default=None,
                        help="cosine similarity floor for an excerpt (default: llm_min_score)")
    parser.add_argument("--contains", default=None,
                        help="require this exact substring in a chunk (e.g. a rule number)")
    parser.add_argument("--llm", choices=["ollama", "claude", "none"], default=None,
                        help="override llm_backend for this run")
    parser.add_argument("--no-llm", action="store_true",
                        help="retrieval only: return the cited excerpts without synthesis")
    parser.add_argument("--show-context", action="store_true",
                        help="print the full excerpt text for every source")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("-v", "--verbose", action="store_true", help="report the setup on stderr")
    return parser.parse_args(argv)


def open_retriever(config: dict):
    """Validate the optional indexing stack and open the collection."""
    if ingest.chromadb is None or not ingest.Embedder.available():
        print("Query needs sentence-transformers and chromadb installed: "
              "pip install -r requirements.txt", file=sys.stderr)
        return None
    collection = ingest.open_collection(config)
    if collection is None:
        print("Could not open the vector store.", file=sys.stderr)
        return None
    try:
        count = collection.count()
    except Exception as exc:  # missing/corrupt database directory
        print(f"Could not read the collection: {exc}", file=sys.stderr)
        return None
    if not count:
        print(f"Collection '{config.get('collection_name')}' is empty. "
              "Run scripts/incremental_ingest.py first.", file=sys.stderr)
        return None
    return Retriever(config, collection, ingest.Embedder(config)), count


def repl(args, config: dict, retriever: Retriever, llm: Llm | None) -> int:
    print("Interactive KERC query. Type a question, or 'exit' to quit.", flush=True)
    max_chars = config.get("llm_max_context_chars")
    while True:
        try:
            question = input("kerc> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not question:
            continue
        if question.lower() in {"exit", "quit", ":q"}:
            return 0
        result = run_query(question, retriever, llm, top_k=args.top_k,
                           min_score=args.min_score, contains=args.contains,
                           max_context_chars=max_chars)
        if args.json:
            print(json.dumps(result.to_dict(), ensure_ascii=False))
        else:
            print(render_result(result, show_context=args.show_context))
        for warning in result.warnings:
            print(f"Warning: {warning}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    config = ingest.load_config(Path(args.config))

    opened = open_retriever(config)
    if opened is None:
        return 1
    retriever, count = opened

    override = "none" if args.no_llm else args.llm
    llm = select_llm(config, override)
    if args.verbose:
        effective_k = args.top_k or config.get("llm_top_k", DEFAULT_TOP_K)
        print(f"Collection: {count} chunks | top_k={effective_k} | "
              f"llm={(llm.name if llm else 'none')}", file=sys.stderr)

    if args.interactive:
        return repl(args, config, retriever, llm)
    if not args.question:
        print("Provide a question, or use --interactive.", file=sys.stderr)
        return 1

    result = run_query(args.question, retriever, llm, top_k=args.top_k,
                       min_score=args.min_score, contains=args.contains,
                       max_context_chars=config.get("llm_max_context_chars"))
    return emit(result, args)


if __name__ == "__main__":
    sys.exit(main())
