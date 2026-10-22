"""file_type_stats table (migration 0018) - cumulative, all-time
per-file-extension processing counts for the Stats & Activity page. See
the migration header for why these accumulate across sync runs instead
of resetting per-run."""

import sqlite3

from jarvis_server.internal.db._middleware import read_query, write_query


@write_query
def upsert_file_type_stats(conn: sqlite3.Connection, deltas: dict[str, dict], updated_at: str) -> None:
    """`deltas` is one sync run's incremental counts, keyed by extension:
    {"supported": bool, "tried": int, "skipped": int, "failed": int,
    "ignored": int, "bytes": int}. Counts accumulate onto the existing
    row via ON CONFLICT - they never overwrite a prior run's totals."""
    for extension, delta in deltas.items():
        conn.execute(
            """
            INSERT INTO file_type_stats
                (extension, supported, tried_count, skipped_count, failed_count, ignored_count,
                 bytes_processed, last_updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(extension) DO UPDATE SET
                supported = excluded.supported,
                tried_count = tried_count + excluded.tried_count,
                skipped_count = skipped_count + excluded.skipped_count,
                failed_count = failed_count + excluded.failed_count,
                ignored_count = ignored_count + excluded.ignored_count,
                bytes_processed = bytes_processed + excluded.bytes_processed,
                last_updated_at = excluded.last_updated_at
            """,
            (
                extension,
                delta["supported"],
                delta.get("tried", 0),
                delta.get("skipped", 0),
                delta.get("failed", 0),
                delta.get("ignored", 0),
                delta.get("bytes", 0),
                updated_at,
            ),
        )


@read_query
def get_file_type_stats(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM file_type_stats ORDER BY extension").fetchall()


@read_query
def get_processing_totals(conn: sqlite3.Connection) -> sqlite3.Row:
    return conn.execute(
        "SELECT COALESCE(SUM(tried_count),0) AS total_count, "
        "COALESCE(SUM(bytes_processed),0) AS total_bytes FROM file_type_stats"
    ).fetchone()
