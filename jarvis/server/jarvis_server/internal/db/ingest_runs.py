"""ingest_runs table (migration 0016) - one row per ingest run. Replaces
the single-row ingest_progress (migration 0015) - see that migration's
header for why a per-run record is needed. Paired with
ingest_run_phases.py for the per-phase detail within a run."""

import sqlite3
from typing import Optional

from jarvis_server.internal.db._middleware import read_query, write_query

STATUS_IN_PROGRESS = "in_progress"
STATUS_DONE = "done"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"
STATUS_INTERRUPTED = "interrupted"


@write_query
def insert_ingest_run(
    conn: sqlite3.Connection, run_id: str, connector: str, root_id: str, trigger: str, started_at: str,
) -> None:
    """Called once at the top of pipeline.run_ingest, before the first
    phase starts."""
    conn.execute(
        "INSERT INTO ingest_runs (id, connector, root_id, trigger, status, started_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (run_id, connector, root_id, trigger, STATUS_IN_PROGRESS, started_at),
    )


@write_query
def finish_ingest_run(
    conn: sqlite3.Connection, run_id: str, status: str, ended_at: str, error_code: Optional[str] = None,
) -> None:
    """`status` is one of STATUS_DONE/STATUS_FAILED/STATUS_CANCELLED -
    whichever way pipeline.run_ingest's own try/except resolves.
    `error_code` (an errors.ErrorCode value) only for STATUS_FAILED."""
    conn.execute(
        "UPDATE ingest_runs SET status = ?, ended_at = ?, error_code = ? WHERE id = ?",
        (status, ended_at, error_code, run_id),
    )


@read_query
def get_ingest_run(conn: sqlite3.Connection, run_id: str) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT id, connector, root_id, trigger, status, started_at, ended_at, error_code "
        "FROM ingest_runs WHERE id = ?",
        (run_id,),
    ).fetchone()


@read_query
def get_recent_ingest_runs(conn: sqlite3.Connection, limit: int = 20) -> list[sqlite3.Row]:
    """Most recent runs first. Not surfaced in any UI yet - schema-ahead-
    of-UI, for the "past runs" browsing view the map defers."""
    return conn.execute(
        "SELECT id, connector, root_id, trigger, status, started_at, ended_at, error_code "
        "FROM ingest_runs ORDER BY started_at DESC LIMIT ?",
        (limit,),
    ).fetchall()


@write_query
def mark_orphaned_ingest_runs_interrupted(conn: sqlite3.Connection, ended_at: str) -> int:
    """Run once at daemon startup (server.py). A row still STATUS_IN_PROGRESS
    at that point belongs to a process that died before reaching a
    terminal state - the daemon's single-writer lock means a genuinely
    live run would require that same process, which just started.
    Returns the number of rows swept, for a startup log line."""
    cursor = conn.execute(
        "UPDATE ingest_runs SET status = ?, ended_at = ? WHERE status = ?",
        (STATUS_INTERRUPTED, ended_at, STATUS_IN_PROGRESS),
    )
    return cursor.rowcount
