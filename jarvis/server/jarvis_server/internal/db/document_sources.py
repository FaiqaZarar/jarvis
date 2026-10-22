"""document_sources table."""

import sqlite3
from typing import Optional

from jarvis_server.internal.db._middleware import read_query, write_query


@write_query
def insert_document_source(
    conn: sqlite3.Connection,
    source_id: str,
    connector: str,
    content_hash: str,
    source_last_seen: str,
    source_url: Optional[str] = None,
    author: Optional[str] = None,
    source_created_at: Optional[str] = None,
    source_modified_at: Optional[str] = None,
    mime: Optional[str] = None,
    native_metadata_json: Optional[str] = None,
    source_mtime: Optional[float] = None,
    source_size: Optional[int] = None,
    source_inode: Optional[str] = None,
    title: Optional[str] = None,
) -> None:
    conn.execute(
        """
        INSERT INTO document_sources (
            source_id, connector, content_hash, source_url, author,
            source_created_at, source_modified_at, mime, source_last_seen,
            native_metadata_json, source_mtime, source_size, source_inode, title
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(connector, source_id) DO UPDATE SET
            content_hash = excluded.content_hash,
            source_last_seen = excluded.source_last_seen,
            source_modified_at = excluded.source_modified_at,
            source_mtime = excluded.source_mtime,
            source_size = excluded.source_size,
            source_inode = excluded.source_inode,
            title = excluded.title,
            mime = excluded.mime,
            tombstoned = 0
        """,
        (
            source_id, connector, content_hash, source_url, author,
            source_created_at, source_modified_at, mime, source_last_seen,
            native_metadata_json, source_mtime, source_size, source_inode, title,
        ),
    )


@read_query
def get_active_document_sources(conn: sqlite3.Connection, connector: str) -> list[sqlite3.Row]:
    """Full rows (not just ids) for every non-tombstoned source of a connector.

    Used by the filesystem connector to build its fast-path-skip and
    rename-detection indexes before a Phase A pull (spec §8.2).
    """
    return conn.execute(
        """
        SELECT source_id, content_hash, source_mtime, source_size, source_inode
        FROM document_sources
        WHERE connector = ? AND tombstoned = 0
        """,
        (connector,),
    ).fetchall()


@read_query
def get_active_document_source_modified_ats(
    conn: sqlite3.Connection, connector: str
) -> dict[str, str]:
    """source_id -> source_modified_at for every non-tombstoned row of a
    connector. Mirrors get_active_document_sources's batch-read pattern,
    for connectors (Drive) whose only native change-detection signal is
    source_modified_at, not the filesystem-specific
    source_mtime/source_size/source_inode fingerprint."""
    rows = conn.execute(
        """
        SELECT source_id, source_modified_at
        FROM document_sources
        WHERE connector = ? AND tombstoned = 0
        """,
        (connector,),
    ).fetchall()
    return {row["source_id"]: row["source_modified_at"] for row in rows}


@write_query
def touch_document_source_seen(
    conn: sqlite3.Connection, connector: str, source_id: str, source_last_seen: str
) -> None:
    """Refresh source_last_seen only — the fast-path-skip case (spec §8.2):
    (mtime, size) matched the stored fingerprint, so content is assumed
    unchanged and nothing else about the row needs to change."""
    conn.execute(
        "UPDATE document_sources SET source_last_seen = ? WHERE connector = ? AND source_id = ?",
        (source_last_seen, connector, source_id),
    )


@write_query
def rename_document_source(
    conn: sqlite3.Connection,
    connector: str,
    old_source_id: str,
    new_source_id: str,
    content_hash: str,
    source_last_seen: str,
    source_modified_at: Optional[str],
    source_mtime: Optional[float],
    source_size: Optional[int],
    source_inode: Optional[str],
    title: Optional[str] = None,
    mime: Optional[str] = None,
) -> None:
    """Repoint an existing row's source_id in place on an inode-matched
    rename (spec §8.2), instead of tombstoning the old path and inserting a
    fresh row under the new one — (st_dev, st_ino) is only ever a rename
    *signal*; source_id (the path) stays the actual identity key.

    title and mime are updated here too: both are derived from the filename
    / extension, so a rename can change the correct value (e.g. `.txt` ->
    `.md`); leaving the old one in place would silently go stale."""
    conn.execute(
        """
        UPDATE document_sources SET
            source_id = ?,
            content_hash = ?,
            source_last_seen = ?,
            source_modified_at = ?,
            source_mtime = ?,
            source_size = ?,
            source_inode = ?,
            title = ?,
            mime = ?,
            tombstoned = 0
        WHERE connector = ? AND source_id = ?
        """,
        (
            new_source_id, content_hash, source_last_seen, source_modified_at,
            source_mtime, source_size, source_inode, title, mime, connector, old_source_id,
        ),
    )


@read_query
def get_document_sources_for_document(
    conn: sqlite3.Connection, content_hash: str
) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM document_sources WHERE content_hash = ?", (content_hash,)
    ).fetchall()


@read_query
def get_live_sources_for_document(conn: sqlite3.Connection, content_hash: str) -> list[sqlite3.Row]:
    """Non-tombstoned sources for a document, in a stable order - the
    provenance list a search/hierarchy ChunkHit reports (mcp_tools's
    _build_chunk_hit)."""
    return conn.execute(
        """
        SELECT source_id, connector, title, source_url, source_last_seen, source_modified_at
        FROM document_sources
        WHERE content_hash = ? AND tombstoned = 0
        ORDER BY id
        """,
        (content_hash,),
    ).fetchall()


@read_query
def get_active_source_ids_for_connector(conn: sqlite3.Connection, connector: str) -> set[str]:
    """All non-tombstoned source_ids currently known for a connector.

    Used by a connector's Phase B sweep (spec §5.3) to figure out which
    previously-seen items are absent from the current pull and should be
    tombstoned.
    """
    rows = conn.execute(
        "SELECT source_id FROM document_sources WHERE connector = ? AND tombstoned = 0",
        (connector,),
    ).fetchall()
    return {row["source_id"] for row in rows}


@write_query
def tombstone_document_source(conn: sqlite3.Connection, connector: str, source_id: str) -> None:
    """Mark a document_source as vanished. Never hard-deletes (spec §5.2) -
    the row and its Document stay for lineage and cheap re-appearance."""
    conn.execute(
        "UPDATE document_sources SET tombstoned = 1 WHERE connector = ? AND source_id = ?",
        (connector, source_id),
    )
