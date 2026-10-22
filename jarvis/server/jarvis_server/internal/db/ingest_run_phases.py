"""ingest_run_phases table (migration 0016) - one row per phase per
ingest run (see ingest_runs.py). A phase never reached simply has no
row - see the migration header for why that's enough to mean "skipped",
with no separate status value for it."""

import sqlite3
from typing import Optional

from jarvis_server.internal.db._middleware import read_query, write_query

STATUS_IN_PROGRESS = "in_progress"
STATUS_DONE = "done"
STATUS_FAILED = "failed"


@write_query
def insert_ingest_run_phase(conn: sqlite3.Connection, run_id: str, phase: str, started_at: str) -> None:
    conn.execute(
        "INSERT INTO ingest_run_phases (run_id, phase, status, started_at) VALUES (?, ?, ?, ?)",
        (run_id, phase, STATUS_IN_PROGRESS, started_at),
    )


@write_query
def set_ingest_run_phase_progress(
    conn: sqlite3.Connection,
    run_id: str,
    phase: str,
    total_items: int,
    processed_items: int,
    total_batches: int,
    current_batch: int,
    updated_at: str,
) -> None:
    """Batch-level count update within a still-in-progress phase - only
    embed_and_index_chunks calls this today (see the migration header on
    why the other phases don't). Committed once per batch, same
    live-visible-to-a-second-connection rationale ingest_progress had."""
    conn.execute(
        """
        UPDATE ingest_run_phases
        SET total_items = ?, processed_items = ?, total_batches = ?, current_batch = ?, updated_at = ?
        WHERE run_id = ? AND phase = ?
        """,
        (total_items, processed_items, total_batches, current_batch, updated_at, run_id, phase),
    )


@write_query
def finish_ingest_run_phase(
    conn: sqlite3.Connection,
    run_id: str,
    phase: str,
    status: str,
    ended_at: str,
    total_items: Optional[int] = None,
    processed_items: Optional[int] = None,
) -> None:
    """`total_items`/`processed_items` are only passed by phases that
    don't already carry live counts from set_ingest_run_phase_progress
    (chunk reports its own count here since it's a single synchronous
    step, not batched) - `None` leaves whatever's already there."""
    conn.execute(
        """
        UPDATE ingest_run_phases
        SET status = ?, ended_at = ?,
            total_items = COALESCE(?, total_items),
            processed_items = COALESCE(?, processed_items)
        WHERE run_id = ? AND phase = ?
        """,
        (status, ended_at, total_items, processed_items, run_id, phase),
    )


@read_query
def get_ingest_run_phases(conn: sqlite3.Connection, run_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT phase, status, total_items, processed_items, total_batches, current_batch, "
        "started_at, updated_at, ended_at FROM ingest_run_phases WHERE run_id = ? ORDER BY started_at",
        (run_id,),
    ).fetchall()
