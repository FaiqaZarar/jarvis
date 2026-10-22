"""Accumulates what the read tools returned over a session and renders it
as an answer-provenance receipt (docs/answer-provenance-DESIGN.md).

Owned by the stdio proxy, not the daemon: one daemon serves every client
at once, so daemon-side accumulation would need invented session identity;
the proxy is already one process per MCP client session.
"""

import html
import re
from typing import Optional

from jarvis_server.mcp_tools.models import (
    ChunkHit,
    ProvenanceResult,
    QueryResult,
    SearchResult,
)

MAX_SNIPPET_CHARS = 280
MAX_SNIPPETS_PER_DOCUMENT = 3
MAX_DOCUMENTS = 25

# Documents scoring below this fraction of the top hit are counted but not
# detailed. search() normalizes RRF against 2/(RRF_K+1), so a chunk found by
# both BM25 and vector search scores ~0.5-1.0 while a single-list long-tail
# hit lands ~0.28-0.45: the floor separates those two populations rather
# than imposing an arbitrary cut. Relative, not absolute, so a query where
# nothing scores well does not silently report nothing.
RELATIVE_SCORE_FLOOR = 0.5

# Ingested Markdown keeps its YAML frontmatter, so a document's first chunk
# is often pure metadata - accurate provenance, useless as evidence.
_FRONTMATTER = re.compile(r"\A\s*---\s*\n.*?\n---\s*(\n|\Z)", re.DOTALL)

# Chunk text carries HTML entities and Markdown escapes that read as literal
# `&nbsp;` / `\-` once quoted into a receipt.
_MD_ESCAPE = re.compile(r"\\([\\`*_{}\[\]()#+\-.!>])")

# query_db is also how a caller discovers the schema; those calls back no
# claim, so they are not provenance.
_INTROSPECTION = re.compile(
    r"\bsqlite_master\b|\bpragma\b"
    r"|\Aselect\s+\*\s+from\s+\S+\s+limit\s+\d+\s*;?\Z",
    re.IGNORECASE,
)


def _clean(text: str) -> str:
    text = _FRONTMATTER.sub("", text)
    text = html.unescape(text).replace("\xa0", " ")
    return _MD_ESCAPE.sub(r"\1", text)


def _truncate(text: str, limit: int) -> str:
    collapsed = " ".join(text.split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: limit - 1].rstrip() + "…"


# title/topic_path/source_label all ultimately trace back to ingested
# content - a topic name is LLM-generated from ingested passages
# (clustering.py), a document title is a filename or a Drive doc name,
# a source label embeds a filesystem path or URL. None of that is
# escaped before landing in this receipt's Markdown, unlike snippets
# (_truncate above already collapses their newlines). A crafted title
# containing "\n- Source: ...\n  > ..." could fabricate an extra,
# convincing-looking receipt line the reader (and the model reading its
# own tool output) would attribute to Jarvis's own provenance machinery,
# not to the document. _truncate already collapses newlines/length; this
# additionally strips characters that open a Markdown block construct at
# the start of a line.
_BLOCK_OPENERS = re.compile(r"^[#>\-*`]+\s*")


def _inline(text: str, limit: int) -> str:
    return _BLOCK_OPENERS.sub("", _truncate(text, limit))


class _DocumentEvidence:
    """One document's evidence, merged across every call that surfaced it -
    the same document hitting three searches in a turn is one source, not
    three."""

    def __init__(self, document_id: str, title: Optional[str]) -> None:
        self.document_id = document_id
        self.title = title
        self.topic_path: Optional[str] = None
        self.queries: list[str] = []
        self.snippets: list[str] = []
        self.best_score: Optional[float] = None
        self.source_label: Optional[str] = None

    def add_hit(self, hit: ChunkHit, query: str) -> None:
        if query not in self.queries:
            self.queries.append(query)
        if self.topic_path is None and hit.topic is not None:
            self.topic_path = hit.topic.path
        if hit.score is not None and (self.best_score is None or hit.score > self.best_score):
            self.best_score = hit.score
        if self.source_label is None and hit.sources:
            source = hit.sources[0]
            # SourceRef.path is filesystem-only; every other connector's
            # source_id is an opaque id that means nothing to a reader.
            locator = source.path or source.url
            self.source_label = (
                f"{source.connector} · {locator}" if locator else source.connector
            )
        if len(self.snippets) < MAX_SNIPPETS_PER_DOCUMENT:
            snippet = _truncate(_clean(hit.text), MAX_SNIPPET_CHARS)
            if snippet and snippet not in self.snippets:
                self.snippets.append(snippet)


class ProvenanceAccumulator:
    """Records tool results; `snapshot_and_reset` renders and clears them.

    The reset point is the turn boundary, because MCP exposes no real one.
    A turn that never calls show_provenance leaves its entries for the next
    receipt to over-report - visible and self-correcting, unlike dropping
    them on a guessed boundary.

    `record_*` runs on the hot path of every tool call and never raises: a
    malformed response must not fail a search that otherwise succeeded.
    """

    def __init__(self) -> None:
        self._documents: dict[str, _DocumentEvidence] = {}
        self._searches: list[tuple[str, int]] = []
        self._sql_calls: list[tuple[str, int]] = []

    @property
    def is_empty(self) -> bool:
        return not self._searches and not self._sql_calls

    @property
    def call_count(self) -> int:
        return len(self._searches) + len(self._sql_calls)

    def record_search(self, result: SearchResult) -> None:
        try:
            self._searches.append((result.query, len(result.results)))
            for hit in result.results:
                document_id = hit.document.document_id
                evidence = self._documents.get(document_id)
                if evidence is None:
                    if len(self._documents) >= MAX_DOCUMENTS:
                        continue
                    evidence = _DocumentEvidence(document_id, hit.document.title)
                    self._documents[document_id] = evidence
                evidence.add_hit(hit, result.query)
        except AttributeError:
            return

    def record_query_db(self, sql: str, result: QueryResult) -> None:
        # No document grouping: query_db returns arbitrary projections with
        # no reliable document identity, and guessing at one is the app-level
        # enrichment sql_query.py deliberately refuses. The SQL is the
        # provenance.
        normalized = " ".join(sql.split())
        if _INTROSPECTION.search(normalized):
            return
        try:
            self._sql_calls.append((normalized, result.row_count))
        except AttributeError:
            return

    def snapshot_and_reset(self) -> ProvenanceResult:
        result = self.render()
        self._documents = {}
        self._searches = []
        self._sql_calls = []
        return result

    def render(self) -> ProvenanceResult:
        if self.is_empty:
            return ProvenanceResult(
                markdown=(
                    "## Provenance\n\n"
                    "No Jarvis lookups have run since the last receipt - this "
                    "answer is not backed by the indexed corpus."
                ),
                call_count=0,
                document_count=0,
            )

        lines: list[str] = ["## Provenance", ""]

        if self._searches:
            lines += ["**Searches run**", ""]
            for query, hit_count in self._searches:
                plural = "" if hit_count == 1 else "s"
                lines.append(f"- `{query}` — {hit_count} result{plural}")
            lines.append("")

        if self._sql_calls:
            lines += ["**SQL queries run**", ""]
            for sql, row_count in self._sql_calls:
                plural = "" if row_count == 1 else "s"
                lines.append(f"- `{_truncate(sql, 200)}` — {row_count} row{plural}")
            lines.append("")

        if self._documents:
            lines += ["**Sources**", ""]
            # Stable sort keeps unscored and tied documents in the order
            # Jarvis returned them.
            ordered = sorted(
                self._documents.values(),
                key=lambda d: -(d.best_score if d.best_score is not None else 0.0),
            )
            shown, omitted = self._apply_score_floor(ordered)
            for evidence in shown:
                title = _inline(evidence.title, 120) if evidence.title else (
                    f"(untitled document {evidence.document_id})"
                )
                lines.append(f"**{title}**")
                if evidence.topic_path:
                    lines.append(f"- Topic: {_inline(evidence.topic_path, 200)}")
                if evidence.source_label:
                    lines.append(f"- Source: {_inline(evidence.source_label, 300)}")
                if evidence.queries:
                    matched = ", ".join(f"`{q}`" for q in evidence.queries)
                    lines.append(f"- Matched by: {matched}")
                lines += [f"  > {snippet}" for snippet in evidence.snippets]
                lines.append("")

            if omitted:
                plural = "" if omitted == 1 else "s"
                lines += [
                    f"_{omitted} further result{plural} scored below the "
                    f"relevance floor and {'is' if omitted == 1 else 'are'} "
                    "not detailed above._",
                    "",
                ]

        return ProvenanceResult(
            markdown="\n".join(lines).rstrip() + "\n",
            call_count=self.call_count,
            document_count=len(self._documents),
        )

    @staticmethod
    def _apply_score_floor(
        ordered: list["_DocumentEvidence"],
    ) -> tuple[list["_DocumentEvidence"], int]:
        """Split score-ordered documents into those worth detailing and a
        count of the rest. Unscored documents are always kept - a missing
        score is not evidence of irrelevance."""
        scores = [d.best_score for d in ordered if d.best_score is not None]
        if not scores:
            return ordered, 0
        floor = max(scores) * RELATIVE_SCORE_FLOOR
        shown = [d for d in ordered if d.best_score is None or d.best_score >= floor]
        return shown, len(ordered) - len(shown)
