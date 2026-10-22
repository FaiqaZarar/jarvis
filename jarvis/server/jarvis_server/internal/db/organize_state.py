"""organize_state table (migration 0011 - see docs/LOW_CONFIDENCE_DESIGN.md)."""

import sqlite3
from typing import Optional

from jarvis_server.internal.db._middleware import read_query, write_query


@read_query
def get_organize_state(conn: sqlite3.Connection) -> tuple[bool, Optional[str]]:
    """The last organize run's small-corpus confidence assessment.

    Returns (low_confidence, low_confidence_reason). `(False, None)` when
    no row has ever been written - the honest default before the first
    Phase B run has happened (nothing has told us to distrust the
    hierarchy). Single-row table (id = 1), like `sync_state`.
    """
    row = conn.execute(
        "SELECT low_confidence, low_confidence_reason FROM organize_state WHERE id = 1"
    ).fetchone()
    if row is None:
        return False, None
    return bool(row["low_confidence"]), row["low_confidence_reason"]


@write_query
def set_organize_state(
    conn: sqlite3.Connection,
    low_confidence: bool,
    reason: Optional[str],
    updated_at: str,
) -> None:
    """Upsert the current organize-run confidence assessment. Written by
    `pipeline._run_reconcile` after every real organize+reconcile run."""
    conn.execute(
        """
        INSERT INTO organize_state (id, low_confidence, low_confidence_reason, updated_at)
        VALUES (1, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            low_confidence = excluded.low_confidence,
            low_confidence_reason = excluded.low_confidence_reason,
            updated_at = excluded.updated_at
        """,
        (1 if low_confidence else 0, reason, updated_at),
    )
