"""tool_calls table (migration 0017) - a persisted log of MCP tool calls
and the pipeline's LLM calls, for later quality review from the desktop
app's Stats & Activity page. See the migration header for retention/
truncation rationale."""

import json
import sqlite3
from typing import Optional

from jarvis_server.internal.db._middleware import read_query, write_query

_MAX_ROWS = 10_000
_MAX_FIELD_BYTES = 10_000


def _truncate(payload) -> str:
    serialized = json.dumps(payload, default=str)
    if len(serialized) > _MAX_FIELD_BYTES:
        return serialized[:_MAX_FIELD_BYTES] + "...<truncated>"
    return serialized


@write_query
def insert_tool_call(
    conn: sqlite3.Connection,
    call_type: str,
    source: str,
    request,
    response,
    success: bool,
    started_at: str,
    *,
    error_message: Optional[str] = None,
    duration_ms: Optional[int] = None,
) -> None:
    """Inserts one row, then prunes back down to the most recent
    _MAX_ROWS - both in this one write_query-wrapped call so they commit
    together rather than leaving a window where the table is over cap."""
    conn.execute(
        "INSERT INTO tool_calls "
        "(call_type, source, request, response, success, error_message, started_at, duration_ms) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            call_type,
            source,
            _truncate(request),
            _truncate(response),
            success,
            error_message,
            started_at,
            duration_ms,
        ),
    )
    conn.execute(
        "DELETE FROM tool_calls WHERE id NOT IN (SELECT id FROM tool_calls ORDER BY id DESC LIMIT ?)",
        (_MAX_ROWS,),
    )


@read_query
def get_recent_tool_calls(
    conn: sqlite3.Connection, limit: int = 50, before_id: Optional[int] = None,
) -> list[sqlite3.Row]:
    """Most recent first. `before_id` is the cursor for pagination - pass
    the smallest id already seen to get the next older page."""
    if before_id is not None:
        return conn.execute(
            "SELECT * FROM tool_calls WHERE id < ? ORDER BY id DESC LIMIT ?",
            (before_id, limit),
        ).fetchall()
    return conn.execute(
        "SELECT * FROM tool_calls ORDER BY id DESC LIMIT ?",
        (limit,),
    ).fetchall()
