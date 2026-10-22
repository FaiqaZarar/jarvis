"""documents table."""

import sqlite3
from typing import Optional

from jarvis_server.internal.db._middleware import read_query, write_query


@write_query
def insert_document(
    conn: sqlite3.Connection,
    content_hash: str,
    normalized_markdown: str,
    first_seen_at: str,
    last_ingested_at: str,
) -> None:
    conn.execute(
        """
        INSERT INTO documents (content_hash, normalized_markdown, first_seen_at, last_ingested_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(content_hash) DO NOTHING
        """,
        (content_hash, normalized_markdown, first_seen_at, last_ingested_at),
    )


@read_query
def get_document(conn: sqlite3.Connection, content_hash: str) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM documents WHERE content_hash = ?", (content_hash,)
    ).fetchone()


@read_query
def get_unchunked_documents(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Every document with no rows in `chunks` yet - the pipeline's
    idempotent chunking pass reads this, so a re-run over an unchanged
    corpus finds nothing here and creates nothing."""
    return conn.execute(
        """
        SELECT content_hash, normalized_markdown
        FROM documents
        WHERE content_hash NOT IN (SELECT DISTINCT content_hash FROM chunks)
        """
    ).fetchall()
