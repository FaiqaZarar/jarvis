"""get_document MCP tool: one document's full text.

Same approach as test_sql_query.py - a real migrated SQLite file under
tmp_path, calling `_get_document_impl` directly.
"""

import sqlite3
from pathlib import Path

import pytest

from jarvis_server.internal import db
from jarvis_server.internal.migrate import run_migrations
from jarvis_server.mcp_tools import document as document_mod
from jarvis_server.mcp_tools.document import MAX_DOCUMENT_CHARS, _get_document_impl, get_document
from jarvis_server.mcp_tools.models import DocumentResult, ErrorResponse

LONG_BODY = "x" * (MAX_DOCUMENT_CHARS + 500)


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "test.db"
    run_migrations(path)
    conn = db.get_connection(path)
    try:
        rows = [
            ("h1", "Full body of the November update.", "Investor Update 23rd November 2020", "2020-11-23"),
            ("h2", "Older body.", "Investor Update 9th November 2020", "2020-11-07"),
            ("h3", LONG_BODY, "Enormous Document", "2021-01-01"),
        ]
        for content_hash, markdown, title, modified in rows:
            conn.execute(
                "INSERT INTO documents (content_hash, normalized_markdown, first_seen_at, last_ingested_at) "
                "VALUES (?, ?, 'ingest-run', 'ingest-run')",
                (content_hash, markdown),
            )
            conn.execute(
                "INSERT INTO document_sources (source_id, connector, content_hash, title, "
                "source_modified_at, source_last_seen, tombstoned) "
                "VALUES (?, 'filesystem', ?, ?, ?, 'now', 0)",
                (f"/docs/{content_hash}.md", content_hash, title, modified),
            )
        conn.commit()
    finally:
        conn.close()
    return path


def test_lookup_by_document_id_returns_full_text(db_path):
    result = _get_document_impl(db_path, document_id="h1")
    assert isinstance(result, DocumentResult)
    assert result.markdown == "Full body of the November update."
    assert result.truncated is False
    assert result.path == "/docs/h1.md"


def test_exact_title_wins_over_substring(db_path):
    result = _get_document_impl(db_path, title="Investor Update 9th November 2020")
    assert result.title == "Investor Update 9th November 2020"
    assert result.other_matches == []


def test_substring_title_returns_newest_and_names_the_rest(db_path):
    result = _get_document_impl(db_path, title="Investor Update")
    # Newest by source_modified_at, not by ingestion order.
    assert result.title == "Investor Update 23rd November 2020"
    assert [m.title for m in result.other_matches] == ["Investor Update 9th November 2020"]


def test_oversized_document_is_capped_and_flagged(db_path):
    result = _get_document_impl(db_path, title="Enormous Document")
    assert result.truncated is True
    assert len(result.markdown) == MAX_DOCUMENT_CHARS


def test_missing_document_is_an_error_not_an_exception(db_path):
    result = _get_document_impl(db_path, title="No Such Thing")
    assert isinstance(result, ErrorResponse)
    assert result.error.code == "not_found"


def test_no_selector_is_rejected(db_path):
    result = _get_document_impl(db_path)
    assert isinstance(result, ErrorResponse)
    assert result.error.code == "invalid_request"


def test_db_error_returns_a_generic_message_not_the_raw_exception(db_path, monkeypatch):
    """Regression: this used to be the one tool in mcp_tools/ that put the
    raw sqlite3 exception (which can contain a real absolute db path) in
    front of an MCP client - every sibling (search, sql_query, hierarchy)
    already logs the real exception and returns a fixed, generic message."""
    def _boom(*a, **k):
        raise sqlite3.OperationalError(f"disk I/O error reading {db_path}")

    monkeypatch.setattr(document_mod, "_get_document_impl", _boom)

    result = get_document(document_id="h1")

    assert isinstance(result, ErrorResponse)
    assert result.error.code == "internal_error"
    assert str(db_path) not in result.error.message
    assert result.error.message == "get_document failed due to an internal error"
