"""organize_runs table (migration 0012).

One row per organize()+reconcile() attempt - see the migration's header
comment for the status-transition discipline this module's callers must
follow (never mark 'completed' before reconcile's persistence succeeds).
"""

import sqlite3
from typing import Optional

from jarvis_server.internal.db._middleware import read_query, write_query


@write_query
def insert_organize_run(
    conn: sqlite3.Connection,
    run_id: str,
    config_version_id: int,
    started_at: str,
) -> None:
    conn.execute(
        "INSERT INTO organize_runs (id, config_version_id, status, started_at) "
        "VALUES (?, ?, 'in_progress', ?)",
        (run_id, config_version_id, started_at),
    )


@read_query
def get_in_progress_organize_run(conn: sqlite3.Connection) -> Optional[sqlite3.Row]:
    """The most recent 'in_progress' run, or None. At most one is ever
    genuinely in flight at a time (the daemon serializes ingest requests
    through one write lock - design doc §4) - ORDER BY + LIMIT 1 is a
    defensive tie-break, not evidence multiple concurrent runs are an
    expected case."""
    return conn.execute(
        "SELECT * FROM organize_runs WHERE status = 'in_progress' "
        "ORDER BY started_at DESC LIMIT 1"
    ).fetchone()


@write_query
def mark_organize_run_completed(conn: sqlite3.Connection, run_id: str, completed_at: str) -> None:
    conn.execute(
        "UPDATE organize_runs SET status = 'completed', completed_at = ? WHERE id = ?",
        (completed_at, run_id),
    )
