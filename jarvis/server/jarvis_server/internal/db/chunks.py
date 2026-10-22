"""chunks table."""

import sqlite3
from typing import Optional

from jarvis_server.internal.db._middleware import read_query, write_query


@write_query
def insert_chunk(
    conn: sqlite3.Connection,
    chunk_id: str,
    content_hash: str,
    ordinal: int,
    chunk_hash: str,
    text: str = "",
    heading_path: Optional[str] = None,
    embedding_ref: Optional[str] = None,
) -> None:
    conn.execute(
        """
        INSERT INTO chunks (chunk_id, content_hash, ordinal, chunk_hash, text, heading_path, embedding_ref)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(chunk_id) DO NOTHING
        """,
        (chunk_id, content_hash, ordinal, chunk_hash, text, heading_path, embedding_ref),
    )


@read_query
def get_chunks_for_document(conn: sqlite3.Connection, content_hash: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM chunks WHERE content_hash = ? ORDER BY ordinal", (content_hash,)
    ).fetchall()


@read_query
def get_all_chunk_ids(conn: sqlite3.Connection) -> set[str]:
    return {row["chunk_id"] for row in conn.execute("SELECT chunk_id FROM chunks").fetchall()}


@read_query
def get_chunk_ids_for_topic(conn: sqlite3.Connection, topic_id: str) -> set[str]:
    return {
        row["chunk_id"]
        for row in conn.execute(
            "SELECT chunk_id FROM chunks WHERE topic_id = ?", (topic_id,)
        ).fetchall()
    }


@read_query
def get_placed_chunk_ids(conn: sqlite3.Connection) -> set[str]:
    """Chunk ids currently placed in the hierarchy (chunks.topic_id set by
    the last reconcile's cache-sync) - the "before" side of pipeline's
    added/removed diff against the live+embedded set."""
    return {
        row["chunk_id"]
        for row in conn.execute("SELECT chunk_id FROM chunks WHERE topic_id IS NOT NULL").fetchall()
    }


@read_query
def get_live_embedded_chunk_ids(conn: sqlite3.Connection) -> set[str]:
    """Chunk ids that are both (a) currently live - reachable through a
    non-tombstoned document_sources row, the same definition search()
    uses - and (b) embedded. This is exactly the set the organize engine
    clusters over."""
    rows = conn.execute(
        """
        SELECT DISTINCT chunks.chunk_id
        FROM chunks
        JOIN documents ON chunks.content_hash = documents.content_hash
        JOIN document_sources ON documents.content_hash = document_sources.content_hash
            AND document_sources.tombstoned = 0
        JOIN chunk_vectors ON chunks.chunk_id = chunk_vectors.chunk_id
        """
    ).fetchall()
    return {row["chunk_id"] for row in rows}


@write_query
def update_chunk_placement(
    conn: sqlite3.Connection,
    chunk_id: str,
    topic_id: Optional[str],
    confidence: Optional[float],
) -> None:
    """Overwrite a chunk's graph-derived placement cache columns. `None`
    for both clears them - the graph has no current placement for this
    chunk (see graph_db/sync.py, whose own invariant is that this cache
    must never be trusted over the graph if the two disagree)."""
    conn.execute(
        "UPDATE chunks SET topic_id = ?, topic_confidence = ? WHERE chunk_id = ?",
        (topic_id, confidence, chunk_id),
    )


@read_query
def get_chunk_by_id(conn: sqlite3.Connection, chunk_id: str) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM chunks WHERE chunk_id = ?", (chunk_id,)).fetchone()


@read_query
def chunk_exists(conn: sqlite3.Connection, chunk_id: str) -> bool:
    return conn.execute("SELECT 1 FROM chunks WHERE chunk_id = ?", (chunk_id,)).fetchone() is not None


@read_query
def chunk_has_live_source(conn: sqlite3.Connection, chunk_id: str) -> bool:
    """True if the chunk is reachable through at least one non-tombstoned
    document_source - the same 'still in the searchable corpus' test
    search() applies unconditionally (spec §5.2)."""
    row = conn.execute(
        """
        SELECT 1
        FROM chunks c
        JOIN document_sources ds ON ds.content_hash = c.content_hash AND ds.tombstoned = 0
        WHERE c.chunk_id = ?
        LIMIT 1
        """,
        (chunk_id,),
    ).fetchone()
    return row is not None


@read_query
def get_chunk_ids_for_topic_ordered(conn: sqlite3.Connection, topic_id: str) -> list[str]:
    """Direct-member chunk ids for a topic, in a stable order - browse()'s
    pagination reads this (never the subtree, and unlike
    get_chunk_ids_for_topic, order matters here so a cursor's offset means
    the same thing across calls)."""
    rows = conn.execute(
        "SELECT chunk_id FROM chunks WHERE topic_id = ? ORDER BY chunk_id", (topic_id,)
    ).fetchall()
    return [row["chunk_id"] for row in rows]


@read_query
def get_candidate_chunk_ids(
    conn: sqlite3.Connection,
    extra_joins: list[str],
    extra_conditions: list[str],
    extra_params: list,
) -> set[str]:
    """search()'s candidate set: the always-on tombstone exclusion (a
    chunk whose only live provenance link is tombstoned is never a
    candidate, filters or not), plus whatever extra JOIN clauses and WHERE
    conditions the caller layers on for its hard pre-filters
    (topic/source/date_range/file_type - see search.py's
    _build_filter_conditions, which builds these from a SearchFilters
    object so this function never has to know that type exists). Always
    returns a real set, never None - an empty set is a legitimate result,
    not an error (spec §7.3)."""
    query = """
        SELECT DISTINCT chunks.chunk_id
        FROM chunks
        JOIN documents ON chunks.content_hash = documents.content_hash
        JOIN document_sources ON documents.content_hash = document_sources.content_hash
            AND document_sources.tombstoned = 0
    """
    for join in extra_joins:
        query += " " + join
    if extra_conditions:
        query += " WHERE " + " AND ".join(extra_conditions)
    rows = conn.execute(query, extra_params).fetchall()
    return {row["chunk_id"] for row in rows}
