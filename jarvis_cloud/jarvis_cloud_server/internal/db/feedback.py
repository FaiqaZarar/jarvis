"""feedback table access."""

import psycopg

from jarvis_cloud_server.internal.db._middleware import write_query

SEVERITIES = ("minor", "annoying", "blocking")


@write_query
def insert_feedback(
    conn: psycopg.Connection, user_id: str, body: str, severity: str, github_issue_url: str | None,
    client_timestamp: str | None, log_excerpt: str | None,
) -> str:
    row = conn.execute(
        """
        INSERT INTO feedback (user_id, body, severity, github_issue_url, client_timestamp, log_excerpt)
        VALUES (%s, %s, %s, %s, %s, %s)
        RETURNING id
        """,
        (user_id, body, severity, github_issue_url, client_timestamp or None, log_excerpt or None),
    ).fetchone()
    return str(row[0])
