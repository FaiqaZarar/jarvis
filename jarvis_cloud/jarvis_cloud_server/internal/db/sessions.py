"""sessions table access - the bearer-token identity behind /feedback."""

from datetime import datetime, timedelta, timezone

import psycopg

from jarvis_cloud_server.internal.db._middleware import read_query, write_query

SESSION_TTL = timedelta(days=30)


@write_query
def create_session(conn: psycopg.Connection, user_id: str, token_hash: bytes) -> None:
    expires_at = datetime.now(timezone.utc) + SESSION_TTL
    conn.execute(
        "INSERT INTO sessions (user_id, token_hash, expires_at) VALUES (%s, %s, %s)",
        (user_id, token_hash, expires_at),
    )


@read_query
def get_active_session_by_token_hash(conn: psycopg.Connection, token_hash: bytes) -> dict | None:
    """None for a missing, expired, or revoked session - callers (the
    /feedback auth check) don't need to distinguish which, they all mean
    401."""
    row = conn.execute(
        """
        SELECT user_id FROM sessions
        WHERE token_hash = %s AND revoked_at IS NULL AND expires_at > now()
        """,
        (token_hash,),
    ).fetchone()
    if row is None:
        return None
    return {"user_id": str(row[0])}
