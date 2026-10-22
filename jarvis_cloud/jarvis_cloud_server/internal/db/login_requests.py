"""login_requests table access - magic-link token lifecycle."""

from datetime import datetime, timedelta, timezone

import psycopg

from jarvis_cloud_server.internal.db._middleware import read_query, write_query

RATE_LIMIT_WINDOW = timedelta(hours=1)
RATE_LIMIT_MAX_REQUESTS = 5
TOKEN_TTL = timedelta(minutes=15)


@write_query
def create_login_request(conn: psycopg.Connection, email: str, token_hash: bytes) -> str:
    """Returns the new row's id, which doubles as the request_id the
    desktop app polls with."""
    expires_at = datetime.now(timezone.utc) + TOKEN_TTL
    row = conn.execute(
        """
        INSERT INTO login_requests (email, token_hash, expires_at)
        VALUES (%s, %s, %s)
        RETURNING id
        """,
        (email, token_hash, expires_at),
    ).fetchone()
    return str(row[0])


@read_query
def get_login_request(conn: psycopg.Connection, request_id: str) -> dict | None:
    row = conn.execute(
        """
        SELECT id, email, token_hash, expires_at, confirmed_at, consumed_at, user_id
        FROM login_requests WHERE id = %s
        """,
        (request_id,),
    ).fetchone()
    if row is None:
        return None
    return {
        "id": str(row[0]), "email": row[1], "token_hash": bytes(row[2]),
        "expires_at": row[3], "confirmed_at": row[4], "consumed_at": row[5],
        "user_id": str(row[6]) if row[6] else None,
    }


@write_query
def claim_session_delivery(conn: psycopg.Connection, request_id: str) -> str | None:
    """Exactly one /auth/status poll gets to mint+deliver the session for
    a given confirmed login request - see this table's own migration
    comment on session_delivered_at for why. Returns the user_id if this
    call is the one that won the claim, None if the request isn't
    confirmed yet or another poll already claimed it (every subsequent
    poll then just reports "confirmed" with no token)."""
    row = conn.execute(
        """
        UPDATE login_requests
        SET session_delivered_at = now()
        WHERE id = %s AND confirmed_at IS NOT NULL AND session_delivered_at IS NULL
        RETURNING user_id
        """,
        (request_id,),
    ).fetchone()
    return str(row[0]) if row is not None else None


@write_query
def confirm_and_consume(
    conn: psycopg.Connection, request_id: str, token_hash: bytes,
) -> str | None:
    """Atomically confirms + single-use-consumes a login request, before
    any user row exists for it - returns the request's email on success,
    None otherwise (wrong/mismatched token hash, already-consumed, or
    expired). The WHERE clause is the entire single-use enforcement, not
    an application-level check-then-act a race could slip through.
    user_id is deliberately NOT set here (see set_user_id below) - doing
    the user lookup/creation only after this succeeds means a wrong-token
    attempt never touches the users table at all."""
    now = datetime.now(timezone.utc)
    row = conn.execute(
        """
        UPDATE login_requests
        SET confirmed_at = %s, consumed_at = %s
        WHERE id = %s AND token_hash = %s AND consumed_at IS NULL AND expires_at > %s
        RETURNING email
        """,
        (now, now, request_id, token_hash, now),
    ).fetchone()
    return row[0] if row is not None else None


@write_query
def set_user_id(conn: psycopg.Connection, request_id: str, user_id: str) -> None:
    """Called once, immediately after confirm_and_consume succeeds - see
    that function's docstring for why the two are split."""
    conn.execute(
        "UPDATE login_requests SET user_id = %s WHERE id = %s", (user_id, request_id),
    )


@read_query
def count_recent_for_email(conn: psycopg.Connection, email: str) -> int:
    """How many login requests this email has made inside
    RATE_LIMIT_WINDOW - the /auth/request rate-limit check."""
    since = datetime.now(timezone.utc) - RATE_LIMIT_WINDOW
    row = conn.execute(
        "SELECT count(*) FROM login_requests WHERE email = %s AND created_at > %s",
        (email, since),
    ).fetchone()
    return row[0]
