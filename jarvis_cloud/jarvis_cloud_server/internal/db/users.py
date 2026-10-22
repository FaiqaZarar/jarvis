"""users table access - the only place allowed to run SQL against it."""

import psycopg

from jarvis_cloud_server.internal.db._middleware import read_query, write_query


@write_query
def get_or_create_user(conn: psycopg.Connection, email: str) -> str:
    """Returns the user's id, creating the row on first sign-in.
    CITEXT on the column handles case-insensitive matching, so this
    never needs to lower() the email itself."""
    row = conn.execute(
        """
        INSERT INTO users (email) VALUES (%s)
        ON CONFLICT (email) DO UPDATE SET email = EXCLUDED.email
        RETURNING id
        """,
        (email,),
    ).fetchone()
    return str(row[0])


@read_query
def get_user_by_id(conn: psycopg.Connection, user_id: str) -> dict | None:
    row = conn.execute(
        "SELECT id, email, created_at FROM users WHERE id = %s", (user_id,)
    ).fetchone()
    if row is None:
        return None
    return {"id": str(row[0]), "email": row[1], "created_at": row[2]}
