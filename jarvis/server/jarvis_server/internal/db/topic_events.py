"""topic_events table."""

import sqlite3
from typing import Optional

from jarvis_server.internal.db._middleware import read_query, write_query


@write_query
def insert_topic_event(
    conn: sqlite3.Connection,
    topic_id: str,
    sync_id: str,
    event_type: str,
    created_at: str,
    detail_json: Optional[str] = None,
) -> None:
    conn.execute(
        """
        INSERT INTO topic_events (topic_id, sync_id, event_type, detail_json, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (topic_id, sync_id, event_type, detail_json, created_at),
    )


@read_query
def get_events_for_topic(conn: sqlite3.Connection, topic_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM topic_events WHERE topic_id = ? ORDER BY id", (topic_id,)
    ).fetchall()
