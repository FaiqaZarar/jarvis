"""query_db MCP tool: read-only SQL access to the Jarvis SQLite store.

`search` answers semantic "what do I know about X" questions via BM25 +
vector ranking - it cannot answer exact/structural questions ("how many
documents match this title pattern", "list every X", "which is earliest
by date") because ranking returns a relevance-ordered, capped subset, not
an exhaustive, exact result. Those questions need a real query against
`jarvis.db` itself, which is what this tool is for.

Safety: SELECT-only, enforced twice -
  1. a syntactic check on the submitted text (single statement, first
     keyword must be SELECT, no write/DDL/pragma/attach keywords anywhere)
  2. the connection itself is opened via a `mode=ro` SQLite URI, so even a
     validation gap can't actually mutate the database file.
Results are capped and each cell is truncated so a broad query can't dump
the entire corpus into the caller's context (get_hierarchy() hit exactly
this failure mode against a real corpus - a 238K-character response).
"""

import re
import sqlite3
import time
from typing import Optional, Union

from jarvis_server.internal import db
from jarvis_server.internal.logging.logging_config import get_logger
from jarvis_server.mcp_tools.models import ErrorDetail, ErrorResponse, QueryResult
from jarvis_server.mcp_tools.server import server

logger = get_logger(__name__)

# TODO: remove this before moving to prod

DEFAULT_ROW_LIMIT = 50
MAX_ROW_LIMIT = 500
MAX_CELL_CHARS = 500
# `_validate_readonly` only rejects write/DDL statements - it says nothing
# about cost. A recursive CTE (`WITH RECURSIVE ...`) or an unbounded
# cross-join is valid, read-only SQL that can still run forever; confirmed
# directly (a `WITH RECURSIVE` counter was still running after 5s+ with no
# progress_handler). Since this executes inside asyncio.to_thread, which
# has no way to cancel the underlying OS thread, an unbounded query would
# permanently consume one of the daemon's limited thread-pool workers -
# every other route shares that same pool.
QUERY_TIMEOUT_S = 5.0

# First keyword must be one of these (case-insensitive) for the query to
# be considered read-only. `EXPLAIN` is allowed too - it never touches
# data, only the query plan.
_ALLOWED_LEADING_KEYWORDS = ("SELECT", "WITH", "EXPLAIN")

# Anywhere these appear (as whole words) the query is rejected outright,
# even inside a CTE or a `WITH` block - a defense-in-depth belt to the
# read-only-connection suspenders below. Checked against the query with
# string literals stripped out first (see _strip_noncode) - the
# corpus this tool runs against is full of titles like "Investor Update",
# and a naive whole-query scan would reject a completely harmless
# `WHERE title LIKE '%Investor Update%'` for containing the word "Update".
_FORBIDDEN_KEYWORDS = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|REPLACE|TRUNCATE|VACUUM|"
    r"ATTACH|DETACH|PRAGMA|REINDEX|ANALYZE)\b",
    re.IGNORECASE,
)

# Every SQL construct that can hide a keyword/quote/semicolon from the
# checks below without SQLite itself treating it as code: a single-quoted
# string literal ('' is the escaped-quote form), a double-quoted
# identifier (SQLite also accepts "" the same way), a `--` line comment,
# and a `/* */` block comment. One combined, ordered alternation - not
# four independent regexes applied in sequence - because independent
# passes let a quote *inside a comment* (or a comment marker inside a
# quote) fool a later pass into mis-reading it as real code. Confirmed as
# a real bypass before this fix: `SELECT 1 --'\nUNION SELECT 2 --'`
# stripped string literals first, saw no real `'` (the ones present were
# inside `--` comments), and passed `_validate_readonly` clean, even
# though SQLite executes the whole thing as two SELECTs unioned. Each
# alternative starts with a character the others can't (`'`, `"`, `-`,
# `/`), so listing order doesn't affect which one matches - each position
# only ever has one construct that could start there.
_NONCODE_TOKEN = re.compile(
    r"'(?:[^']|'')*'"       # string literal
    r'|"(?:[^"]|"")*"'      # quoted identifier
    r"|--[^\n]*"            # line comment
    r"|/\*.*?\*/",          # block comment
    re.DOTALL,
)


def _strip_noncode(sql: str) -> str:
    def _replace(match: re.Match) -> str:
        text = match.group(0)
        if text[0] == "'":
            return "''"
        if text[0] == '"':
            return '""'
        return " "  # a comment - collapse to whitespace, not deleted, so
        # adjacent tokens (`SELECT--x\nDROP`) don't get glued together

    return _NONCODE_TOKEN.sub(_replace, sql)


def _err(code: str, message: str) -> ErrorResponse:
    return ErrorResponse(error=ErrorDetail(code=code, message=message))


def _validate_readonly(sql: str) -> Optional[str]:
    """Return an error message if `sql` isn't a safe single read-only
    statement, else None."""
    stripped = sql.strip().rstrip(";").strip()
    if not stripped:
        return "sql must not be empty"
    if ";" in _strip_noncode(stripped):
        return "only a single statement is allowed (no ';' outside a quoted string)"

    first_word_match = re.match(r"[A-Za-z]+", stripped)
    first_word = first_word_match.group(0).upper() if first_word_match else ""
    if first_word not in _ALLOWED_LEADING_KEYWORDS:
        return (
            f"query must start with SELECT/WITH/EXPLAIN, not '{first_word or stripped[:20]}'"
        )

    forbidden = _FORBIDDEN_KEYWORDS.search(_strip_noncode(stripped))
    if forbidden:
        return f"'{forbidden.group(0)}' is not allowed - this tool is read-only"

    return None


def _truncate_cell(value) -> tuple[Optional[str], bool]:
    if value is None:
        return None, False
    text = value if isinstance(value, str) else repr(value)
    if len(text) > MAX_CELL_CHARS:
        return text[:MAX_CELL_CHARS] + f"... [truncated, {len(text)} chars total]", True
    return text, False


def _query_impl(db_path, sql: str, limit: int) -> Union[QueryResult, ErrorResponse]:
    error = _validate_readonly(sql)
    if error:
        return _err("invalid_argument", error)

    limit = max(1, min(limit, MAX_ROW_LIMIT))

    # Belt-and-suspenders: even a validation gap can't write, because the
    # connection itself is opened read-only at the SQLite level.
    ro_uri = f"file:{db_path}?mode=ro"
    try:
        conn = sqlite3.connect(ro_uri, uri=True)
    except sqlite3.OperationalError as exc:
        return _err("internal_error", f"could not open database read-only: {exc}")

    deadline = time.monotonic() + QUERY_TIMEOUT_S
    # Checked every 10k VM instructions - frequent enough to cut off a
    # runaway query within a fraction of a second of the deadline, cheap
    # enough (a monotonic() call, no I/O) to not meaningfully slow a
    # normal query down.
    conn.set_progress_handler(lambda: time.monotonic() > deadline, 10_000)

    try:
        cursor = conn.execute(sql)
        columns = [d[0] for d in cursor.description] if cursor.description else []
        fetched = cursor.fetchmany(limit + 1)
        truncated_rows = len(fetched) > limit
        fetched = fetched[:limit]

        rows: list[list[Optional[str]]] = []
        truncated_cells = False
        for row in fetched:
            out_row = []
            for value in row:
                cell, was_truncated = _truncate_cell(value)
                truncated_cells = truncated_cells or was_truncated
                out_row.append(cell)
            rows.append(out_row)

        return QueryResult(
            columns=columns,
            rows=rows,
            row_count=len(rows),
            truncated_rows=truncated_rows,
            truncated_cells=truncated_cells,
        )
    except sqlite3.Error as exc:
        if time.monotonic() > deadline:
            return _err(
                "invalid_argument",
                f"query timed out after {QUERY_TIMEOUT_S}s - narrow it (add a WHERE/LIMIT, "
                "avoid an unbounded recursive CTE or cross join)",
            )
        return _err("invalid_argument", f"query failed: {exc}")
    finally:
        conn.set_progress_handler(None, 0)
        conn.close()


@server.tool()
def query_db(sql: str, limit: int = DEFAULT_ROW_LIMIT) -> Union[QueryResult, ErrorResponse]:
    """Run a read-only SQL SELECT against the Jarvis SQLite database
    directly. Use this for exact/structural questions `search` cannot
    answer reliably: counts ("how many documents match X"), exhaustive
    listings ("list every document titled like Y"), min/max/ordering by a
    real column ("which is earliest by date"), or joins across documents /
    sources / topics. Prefer `search` for semantic "what do I know about
    X" questions - use this one when you need an exact, complete answer
    instead of a relevance-ranked, capped one.

    Read-only: INSERT/UPDATE/DELETE/DROP/ALTER/CREATE/ATTACH/PRAGMA/etc.
    are rejected, and the underlying connection is opened read-only at the
    SQLite level regardless. Only a single SELECT/WITH/EXPLAIN statement
    is accepted (no ';'-separated multi-statement queries).

    Results are capped at 500 rows and each cell at 500 characters -
    `truncated_rows` / `truncated_cells` on the response say if that
    happened; narrow the query (add a WHERE, a LIMIT, or specific columns
    instead of *) rather than raising `limit` for a broad scan.

    Schema:
    documents(content_hash PK, normalized_markdown, first_seen_at, last_ingested_at)
    chunks(chunk_id PK, content_hash FK->documents, ordinal, chunk_hash,
           heading_path, embedding_ref, text, topic_id FK->topics.id,
           topic_confidence)
    document_sources(id PK, source_id, connector, content_hash FK->documents,
           source_url, author, source_created_at, source_modified_at, mime,
           source_last_seen, tombstoned, native_metadata_json, source_mtime,
           source_size, source_inode, title)
    topics(id PK, name, description, centroid, level, version, reserved,
           tombstoned, superseded_by, children_ids, ancestor_path,
           direct_chunk_count, subtree_chunk_count)
    topic_events(id PK, topic_id, sync_id, event_type, detail_json, created_at)
    chunk_vectors(chunk_id, embedding)  -- raw vector blob, not human-readable
    sync_state(id, current_sync_id)
    organize_state(id, low_confidence, low_confidence_reason, updated_at)
    connector_state(connector, key, value, updated_at)

    chunk_context: a read-only VIEW, not a table - join or select from it
    instead of hand-joining chunks/documents/document_sources/topics
    yourself when you need a chunk's provenance (which document, which
    topic, under what connector). One row per chunk:
    chunk_context(chunk_id, content_hash, ordinal, heading_path,
        document_first_seen_at, document_last_ingested_at, source_id,
        connector, document_title, source_url, author,
        source_modified_at, mime, native_metadata_json, topic_id,
        topic_name, topic_path)
    topic_path is the same " > "-joined breadcrumb search()/browse()
    return (root-to-self, synthetic root excluded), pre-computed here so
    you don't need to unpack topics.ancestor_path's JSON yourself.

    Notes: one document's content can have multiple document_sources rows
    (same content ingested from more than one place) - join on
    content_hash. A document's chunks: chunks.content_hash =
    documents.content_hash, ordered by chunks.ordinal. tombstoned=1 means
    soft-deleted - filter it out unless asked to include deleted items.
    """
    try:
        return _query_impl(db.DEFAULT_DB_PATH, sql, limit)
    except Exception:
        logger.exception("query_db() failed unexpectedly for sql=%r", sql)
        return _err("internal_error", "query_db failed due to an internal error")
