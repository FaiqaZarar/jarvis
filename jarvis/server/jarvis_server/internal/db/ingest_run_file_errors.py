"""ingest_run_file_errors table (migration 0020) - the files one ingest run
couldn't read, and why. Local only; see the migration header."""

import sqlite3

from jarvis_server.internal.db._middleware import read_query, write_query


@write_query
def insert_ingest_run_file_errors(conn: sqlite3.Connection, run_id: str, failed_files: list[dict]) -> None:
    """`failed_files` items carry `source_id`, `display_name`, `error_code`
    - the shape connectors collect in their sync stats. A file reported
    twice in one run keeps its first reason."""
    conn.executemany(
        "INSERT OR IGNORE INTO ingest_run_file_errors (run_id, source_id, display_name, error_code) "
        "VALUES (?, ?, ?, ?)",
        [(run_id, f["source_id"], f.get("display_name"), f["error_code"]) for f in failed_files],
    )


@read_query
def get_ingest_run_file_errors(conn: sqlite3.Connection, run_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT source_id, display_name, error_code FROM ingest_run_file_errors "
        "WHERE run_id = ? ORDER BY display_name",
        (run_id,),
    ).fetchall()
