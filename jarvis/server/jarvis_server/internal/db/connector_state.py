"""connector_state table - generic per-connector key/value, survives across
sync runs (migration 0010; J-11 phase 2 uses it for Drive's startPageToken)."""

import sqlite3
from typing import Optional

from jarvis_server.internal.db._middleware import read_query, write_query


@read_query
def get_connector_state(conn: sqlite3.Connection, connector: str, key: str) -> Optional[str]:
    """The stored value for (connector, key), or None if it was never set -
    the correct signal for 'no stored cursor yet, run the full path.'"""
    row = conn.execute(
        "SELECT value FROM connector_state WHERE connector = ? AND key = ?",
        (connector, key),
    ).fetchone()
    return row["value"] if row else None


@write_query
def set_connector_state(
    conn: sqlite3.Connection, connector: str, key: str, value: str, updated_at: str
) -> None:
    """Upsert (connector, key) -> value - the same ON CONFLICT DO UPDATE
    convention as insert_document_source."""
    conn.execute(
        """
        INSERT INTO connector_state (connector, key, value, updated_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(connector, key) DO UPDATE SET
            value = excluded.value,
            updated_at = excluded.updated_at
        """,
        (connector, key, value, updated_at),
    )
