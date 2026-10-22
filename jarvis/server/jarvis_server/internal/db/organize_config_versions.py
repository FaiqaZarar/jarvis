"""organize_config_versions table (migration 0012).

Append-only, never updated or deleted - see the migration's header
comment. Deliberately no "is a version current" logic in this module:
the table just stores rows; jarvis_server.organize.config_versions
decides what "current" means and when a new row is warranted.
"""

import sqlite3
from typing import Optional

from jarvis_server.internal.db._middleware import read_query, write_query


@write_query
def insert_config_version(
    conn: sqlite3.Connection,
    params_json: str,
    created_at: str,
    note: Optional[str] = None,
) -> int:
    cur = conn.execute(
        "INSERT INTO organize_config_versions (params_json, created_at, note) VALUES (?, ?, ?)",
        (params_json, created_at, note),
    )
    return cur.lastrowid


@read_query
def get_current_config_version(conn: sqlite3.Connection) -> Optional[sqlite3.Row]:
    """The most recently inserted row (highest id), or None if the table
    is empty - "current" is simply the latest append, chronological."""
    return conn.execute(
        "SELECT * FROM organize_config_versions ORDER BY id DESC LIMIT 1"
    ).fetchone()
