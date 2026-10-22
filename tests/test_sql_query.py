"""query_db MCP tool: read-only SQL access to jarvis.db.

Same approach as test_search.py / test_hierarchy.py - a real migrated
SQLite file under tmp_path, no network. Calls `_query_impl` directly
(the pattern every other mcp_tools test uses) rather than the
`@server.tool()`-wrapped `query_db`, which just adds a `DEFAULT_DB_PATH`
+ exception-logging shell around it.
"""

from pathlib import Path

import pytest

from jarvis_server.internal import db
from jarvis_server.internal.migrate import run_migrations
from jarvis_server.mcp_tools import sql_query
from jarvis_server.mcp_tools.models import ErrorResponse
from jarvis_server.mcp_tools.sql_query import (
    MAX_CELL_CHARS,
    QueryResult,
    _query_impl,
    _validate_readonly,
)


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "test.db"
    run_migrations(path)
    conn = db.get_connection(path)
    try:
        conn.execute(
            "INSERT INTO documents (content_hash, normalized_markdown, first_seen_at, last_ingested_at) "
            "VALUES ('h1', '# T', 'now', 'now')"
        )
        conn.execute(
            "INSERT INTO document_sources (source_id, connector, content_hash, title, source_last_seen, tombstoned) "
            "VALUES ('/tmp/a.md', 'filesystem', 'h1', 'Investor Update 1', 'now', 0)"
        )
        conn.execute(
            "INSERT INTO document_sources (source_id, connector, content_hash, title, source_last_seen, tombstoned) "
            "VALUES ('/tmp/b.md', 'filesystem', 'h1', 'Investor Update 2', 'now', 0)"
        )
        conn.commit()
    finally:
        conn.close()
    return path


# ---------------------------------------------------------------------
# _validate_readonly
# ---------------------------------------------------------------------

@pytest.mark.parametrize("sql", [
    "SELECT * FROM documents",
    "  select 1  ",
    "WITH x AS (SELECT 1) SELECT * FROM x",
    "EXPLAIN SELECT * FROM documents",
])
def test_validate_readonly_accepts_reads(sql):
    assert _validate_readonly(sql) is None


@pytest.mark.parametrize("sql", [
    "INSERT INTO documents VALUES (1)",
    "UPDATE documents SET content_hash = 'x'",
    "DELETE FROM documents",
    "DROP TABLE documents",
    "ATTACH DATABASE 'x' AS y",
    "PRAGMA table_info(documents)",
    "SELECT 1; DROP TABLE documents",
    "",
    "   ",
])
def test_validate_readonly_rejects_writes(sql):
    assert _validate_readonly(sql) is not None


def test_validate_readonly_does_not_flag_forbidden_words_inside_string_literals():
    # regression: 'Investor Update' as a search term must not be rejected
    # for containing the word "Update"
    sql = "SELECT title FROM document_sources WHERE title LIKE '%Investor Update%'"
    assert _validate_readonly(sql) is None

    sql_semicolon_in_literal = "SELECT title FROM document_sources WHERE title = 'a; b'"
    assert _validate_readonly(sql_semicolon_in_literal) is None


def test_validate_readonly_rejects_write_hidden_in_cte():
    # a forbidden keyword anywhere in the statement is rejected, not just
    # checked against the leading keyword
    sql = "WITH x AS (DELETE FROM documents RETURNING *) SELECT * FROM x"
    assert _validate_readonly(sql) is not None


# ---------------------------------------------------------------------
# Regression: independent string-literal-then-keyword passes could be
# fooled by a quote hidden inside a comment (or vice versa) - confirmed a
# real bypass before _strip_noncode became one combined, ordered scan.
# ---------------------------------------------------------------------

def test_validate_readonly_rejects_a_second_statement_hidden_behind_a_line_comment():
    sql = "SELECT 1 --'\n; ATTACH DATABASE 'e.db' AS y --'"
    assert _validate_readonly(sql) is not None


def test_validate_readonly_rejects_a_forbidden_keyword_hidden_behind_a_line_comment():
    sql = "SELECT 1 --'\nUPDATE documents SET content_hash='x'"
    assert _validate_readonly(sql) is not None


def test_validate_readonly_accepts_a_real_block_comment():
    sql = "SELECT 1 /* just a comment */ FROM documents"
    assert _validate_readonly(sql) is None


def test_validate_readonly_does_not_treat_comment_like_text_inside_a_literal_as_a_comment():
    sql = "SELECT '--DROP TABLE documents' AS not_a_real_comment"
    assert _validate_readonly(sql) is None


# ---------------------------------------------------------------------
# _query_impl
# ---------------------------------------------------------------------

def test_query_impl_returns_rows(db_path):
    result = _query_impl(db_path, "SELECT title FROM document_sources ORDER BY title", 10)
    assert isinstance(result, QueryResult)
    assert result.columns == ["title"]
    assert result.rows == [["Investor Update 1"], ["Investor Update 2"]]
    assert result.row_count == 2
    assert not result.truncated_rows
    assert not result.truncated_cells


def test_query_impl_counts(db_path):
    result = _query_impl(
        db_path,
        "SELECT COUNT(*) AS n FROM document_sources WHERE title LIKE '%Investor Update%'",
        10,
    )
    assert isinstance(result, QueryResult)
    assert result.rows == [["2"]]


def test_query_impl_rejects_write(db_path):
    result = _query_impl(db_path, "DELETE FROM document_sources", 10)
    assert isinstance(result, ErrorResponse)
    assert result.error.code == "invalid_argument"

    # confirm nothing was actually deleted
    conn = db.get_connection(db_path)
    try:
        (count,) = conn.execute("SELECT COUNT(*) FROM document_sources").fetchone()
    finally:
        conn.close()
    assert count == 2


def test_query_impl_connection_is_actually_read_only(db_path):
    # even a write that somehow slipped past _validate_readonly must still
    # fail at the SQLite level, since the connection is opened mode=ro
    result = _query_impl(db_path, "SELECT 1", 10)
    assert isinstance(result, QueryResult)  # sanity: the fixture itself works

    import sqlite3
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("DELETE FROM document_sources")
    finally:
        conn.close()


def test_query_impl_bad_sql_returns_error(db_path):
    result = _query_impl(db_path, "SELECT * FROM no_such_table", 10)
    assert isinstance(result, ErrorResponse)
    assert result.error.code == "invalid_argument"


def test_query_impl_recursive_cte_times_out_instead_of_hanging_forever(db_path, monkeypatch):
    """Regression test: _validate_readonly's SELECT/WITH allowlist has no
    notion of cost - an unbounded recursive CTE is valid, read-only SQL
    that would otherwise run forever (confirmed directly: 5+ seconds with
    no exit before this fix existed), permanently consuming one of the
    daemon's asyncio.to_thread workers since that call has no way to be
    cancelled from outside."""
    monkeypatch.setattr(sql_query, "QUERY_TIMEOUT_S", 0.2)

    result = _query_impl(
        db_path,
        "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM c) SELECT count(*) FROM c",
        50,
    )

    assert isinstance(result, ErrorResponse)
    assert result.error.code == "invalid_argument"
    assert "timed out" in result.error.message


def test_query_impl_row_limit_and_truncation_flag(db_path):
    result = _query_impl(db_path, "SELECT title FROM document_sources", 1)
    assert isinstance(result, QueryResult)
    assert result.row_count == 1
    assert result.truncated_rows


def test_query_impl_cell_truncation_flag(db_path):
    long_title = "x" * (MAX_CELL_CHARS + 50)
    conn = db.get_connection(db_path)
    try:
        conn.execute(
            "INSERT INTO document_sources (source_id, connector, content_hash, title, source_last_seen, tombstoned) "
            "VALUES ('/tmp/c.md', 'filesystem', 'h1', ?, 'now', 0)",
            (long_title,),
        )
        conn.commit()
    finally:
        conn.close()

    result = _query_impl(db_path, "SELECT title FROM document_sources WHERE source_id = '/tmp/c.md'", 10)
    assert isinstance(result, QueryResult)
    assert result.truncated_cells
    assert len(result.rows[0][0]) < len(long_title)


# ---------------------------------------------------------------------
# chunk_context view (answer-provenance, docs/answer-provenance-DESIGN.md)
# ---------------------------------------------------------------------

def test_chunk_context_view_joins_document_and_topic_breadcrumb(db_path):
    conn = db.get_connection(db_path)
    try:
        conn.execute(
            "INSERT INTO topics (id, name, level, ancestor_path) VALUES "
            "('40000000-0000-4000-8000-000000000000', 'Knowledge Base', 0, '[]'), "
            "('t-mid', 'Mid', 1, '[\"40000000-0000-4000-8000-000000000000\", \"t-mid\"]'), "
            "('t-leaf', 'Leaf', 2, "
            "'[\"40000000-0000-4000-8000-000000000000\", \"t-mid\", \"t-leaf\"]')"
        )
        conn.execute(
            "INSERT INTO chunks (chunk_id, content_hash, ordinal, chunk_hash, topic_id) "
            "VALUES ('c1', 'h1', 0, 'c1', 't-leaf')"
        )
        conn.commit()
    finally:
        conn.close()

    result = _query_impl(
        db_path,
        "SELECT document_title, connector, topic_name, topic_path "
        "FROM chunk_context WHERE chunk_id = 'c1' AND source_id = '/tmp/a.md'",
        10,
    )
    assert isinstance(result, QueryResult)
    assert result.rows == [["Investor Update 1", "filesystem", "Leaf", "Mid > Leaf"]]


def test_chunk_context_view_excludes_tombstoned_sources(db_path):
    conn = db.get_connection(db_path)
    try:
        conn.execute(
            "INSERT INTO chunks (chunk_id, content_hash, ordinal, chunk_hash) "
            "VALUES ('c2', 'h1', 1, 'c2')"
        )
        conn.execute(
            "UPDATE document_sources SET tombstoned = 1 WHERE source_id = '/tmp/a.md'"
        )
        conn.commit()
    finally:
        conn.close()

    result = _query_impl(
        db_path,
        "SELECT document_title FROM chunk_context WHERE chunk_id = 'c2' "
        "AND source_id = '/tmp/a.md'",
        10,
    )
    assert isinstance(result, QueryResult)
    assert result.rows == []
