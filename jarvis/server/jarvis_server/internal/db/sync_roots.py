"""sync_roots table - which folder/root each connector has synced cleanly,
and when (migration 0013). Generic across connectors: root_id is whatever
identifier a connector's sync() is scoped to (resolved absolute path for
filesystem, Drive folder id for Drive)."""

import sqlite3
from typing import Optional

from jarvis_server.internal.db._middleware import read_query, write_query


@write_query
def record_sync_root(
    conn: sqlite3.Connection, connector: str, root_id: str, synced_at: str, display_name: Optional[str] = None,
) -> None:
    """Upsert (connector, root_id) -> synced_at. first_synced_at is set
    once and never overwritten; last_synced_at moves on every call. Also
    un-tombstones the row - a fresh sync means it's no longer superseded,
    see tombstone_sync_roots. A None display_name keeps whatever name the
    row already has.

    Callers only call this after a zero-error sync (pipeline.run_ingest) -
    this function has no opinion on that itself, same as
    touch_document_source_seen trusting its caller's fast-skip decision."""
    conn.execute(
        """
        INSERT INTO sync_roots (connector, root_id, first_synced_at, last_synced_at, tombstoned, display_name)
        VALUES (?, ?, ?, ?, 0, ?)
        ON CONFLICT(connector, root_id) DO UPDATE SET
            last_synced_at = excluded.last_synced_at,
            tombstoned = 0,
            display_name = COALESCE(excluded.display_name, sync_roots.display_name)
        """,
        (connector, root_id, synced_at, synced_at, display_name),
    )


@read_query
def get_sync_roots(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Every non-tombstoned tracked (connector, root_id), most recently
    synced first - the debug UI's "Synced roots" panel."""
    return conn.execute(
        "SELECT connector, root_id, first_synced_at, last_synced_at, display_name "
        "FROM sync_roots WHERE tombstoned = 0 ORDER BY last_synced_at DESC"
    ).fetchall()


@write_query
def tombstone_sync_roots(conn: sqlite3.Connection, connector: str, root_ids: list[str]) -> None:
    """Supersede rows covered by a broader root's successful sync - see
    Connector.covered_root_ids. record_sync_root un-tombstones a row if
    that narrower root is ever synced again on its own."""
    conn.executemany(
        "UPDATE sync_roots SET tombstoned = 1 WHERE connector = ? AND root_id = ?",
        [(connector, root_id) for root_id in root_ids],
    )
