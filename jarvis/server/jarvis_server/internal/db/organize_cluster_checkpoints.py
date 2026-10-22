"""organize_cluster_checkpoints table (migration 0012).

Keyed by (run_id, member_fingerprint) - a content fingerprint of a base
cluster's member chunk_ids, not HDBSCAN's own transient label numbering.
See the migration's header comment and
docs/organize-checkpointing-DESIGN.md §2c for why: a fingerprint can
only ever match a cluster with exactly the same membership, so a lookup
either correctly reuses a label or safely falls through to a fresh LLM
call - never attaches a stored label to content it doesn't match.
"""

import sqlite3
from typing import Optional

from jarvis_server.internal.db._middleware import read_query, write_query


@read_query
def get_cluster_checkpoint(
    conn: sqlite3.Connection, run_id: str, member_fingerprint: str
) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM organize_cluster_checkpoints WHERE run_id = ? AND member_fingerprint = ?",
        (run_id, member_fingerprint),
    ).fetchone()


@write_query
def insert_cluster_checkpoint(
    conn: sqlite3.Connection,
    run_id: str,
    member_fingerprint: str,
    label: str,
    description: str,
    created_at: str,
) -> None:
    conn.execute(
        "INSERT INTO organize_cluster_checkpoints "
        "(run_id, member_fingerprint, label, description, created_at) VALUES (?, ?, ?, ?, ?)",
        (run_id, member_fingerprint, label, description, created_at),
    )
