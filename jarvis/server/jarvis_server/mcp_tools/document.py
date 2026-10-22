"""get_document MCP tool: one document's full text.

`query_db` truncates every cell at `MAX_CELL_CHARS` so a broad query can't
dump the corpus, which makes it unable to return a whole document - real
documents here run past that cap routinely. Raising the cap would weaken
the guard for every 50-row query to serve a one-row case, so reading one
document in full is its own tool with its own ceiling.
"""

import sqlite3
from pathlib import Path
from typing import Optional, Union

from jarvis_server.internal import db
from jarvis_server.internal.logging.logging_config import get_logger
from jarvis_server.mcp_tools.models import (
    DocumentMatch,
    DocumentResult,
    ErrorDetail,
    ErrorResponse,
)

logger = get_logger(__name__)

MAX_DOCUMENT_CHARS = 50_000
MAX_OTHER_MATCHES = 10

_SELECT = """
    SELECT d.content_hash, d.normalized_markdown, ds.title, ds.connector,
           ds.source_url, ds.source_id, ds.source_modified_at, cc.topic_path
    FROM documents d
    JOIN document_sources ds ON ds.content_hash = d.content_hash
    LEFT JOIN (
        SELECT content_hash, max(topic_path) AS topic_path
        FROM chunk_context GROUP BY content_hash
    ) cc ON cc.content_hash = d.content_hash
    WHERE ds.tombstoned = 0
"""


def _row_to_result(row: sqlite3.Row, others: list[DocumentMatch]) -> DocumentResult:
    markdown = row["normalized_markdown"] or ""
    truncated = len(markdown) > MAX_DOCUMENT_CHARS
    return DocumentResult(
        document_id=row["content_hash"],
        title=row["title"],
        connector=row["connector"],
        source_url=row["source_url"],
        # source_id is the absolute path for the filesystem connector only.
        path=row["source_id"] if row["connector"] == "filesystem" else None,
        source_modified_at=row["source_modified_at"],
        topic_path=row["topic_path"],
        markdown=markdown[:MAX_DOCUMENT_CHARS],
        truncated=truncated,
        other_matches=others,
    )


def _get_document_impl(
    db_path: Path,
    document_id: Optional[str] = None,
    title: Optional[str] = None,
) -> Union[DocumentResult, ErrorResponse]:
    if not document_id and not title:
        return ErrorResponse(
            error=ErrorDetail(
                code="invalid_request",
                message="Pass either document_id or title.",
            )
        )

    # Read-only URI, same as sql_query.py: this tool never writes.
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        if document_id:
            rows = conn.execute(
                _SELECT + " AND d.content_hash = ? LIMIT 1", (document_id,)
            ).fetchall()
        else:
            # Exact title first: a LIKE on a fully-specified title would
            # also match every longer title containing it.
            rows = conn.execute(
                _SELECT + " AND ds.title = ? ORDER BY ds.source_modified_at DESC",
                (title,),
            ).fetchall()
            if not rows:
                rows = conn.execute(
                    _SELECT + " AND ds.title LIKE ? ORDER BY ds.source_modified_at DESC",
                    (f"%{title}%",),
                ).fetchall()

        if not rows:
            return ErrorResponse(
                error=ErrorDetail(
                    code="not_found",
                    message=f"No document matches {document_id or title!r}.",
                )
            )

        # Newest wins, but the alternatives are named rather than dropped -
        # "the latest investor update" is a title-prefix match over many.
        others = [
            DocumentMatch(
                document_id=r["content_hash"],
                title=r["title"],
                source_modified_at=r["source_modified_at"],
            )
            for r in rows[1 : 1 + MAX_OTHER_MATCHES]
        ]
        return _row_to_result(rows[0], others)
    finally:
        conn.close()


def get_document(
    document_id: Optional[str] = None, title: Optional[str] = None
) -> Union[DocumentResult, ErrorResponse]:
    """One document's full text, by `document_id` (the `content_hash` that
    search results and provenance receipts report) or by `title`.

    A title match prefers an exact hit, falls back to a substring match,
    and returns the most recently modified document with the rest listed in
    `other_matches`.
    """
    try:
        return _get_document_impl(db.DEFAULT_DB_PATH, document_id, title)
    except sqlite3.Error:
        # The only tool in this package that put the raw exception message
        # (a real absolute db path, in some sqlite3 error strings) in front
        # of an MCP client - every sibling (search, sql_query, hierarchy)
        # already logs the real exception and returns a fixed, generic
        # message instead. Matched here, not left as the one exception.
        logger.exception("get_document failed")
        return ErrorResponse(
            error=ErrorDetail(code="internal_error", message="get_document failed due to an internal error"),
        )
