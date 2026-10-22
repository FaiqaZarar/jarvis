"""auto_error_reports table access - automatic client-error reporting,
deduplicated by fingerprint (see migration 0004's own comment for why
dedup is global, not per-user)."""

import psycopg

from jarvis_cloud_server.internal.db._middleware import write_query


@write_query
def record_error_report(
    conn: psycopg.Connection, fingerprint: str, error_type: str, message: str,
) -> tuple[bool, str | None]:
    """Inserts a new row for a never-before-seen fingerprint, or bumps
    `occurrence_count`/`last_seen_at` on an existing one. Returns
    `(is_first_occurrence, existing_issue_url)` - the caller only opens a
    GitHub issue when `is_first_occurrence` is True; otherwise
    `existing_issue_url` is whatever's already on file (may itself be
    None if a prior GitHub call failed - the caller treats that the same
    as "nothing to do," it doesn't retry the GitHub call here).

    `xmax = 0` is the standard postgres idiom for "did this row come from
    the INSERT branch or the DO UPDATE branch" - an inserted row's xmax
    is always 0, an updated row's isn't."""
    row = conn.execute(
        """
        INSERT INTO auto_error_reports (fingerprint, error_type, message)
        VALUES (%s, %s, %s)
        ON CONFLICT (fingerprint) DO UPDATE SET
            occurrence_count = auto_error_reports.occurrence_count + 1,
            last_seen_at = now()
        RETURNING (xmax = 0) AS is_first_occurrence, github_issue_url
        """,
        (fingerprint, error_type, message),
    ).fetchone()
    return bool(row[0]), row[1]


@write_query
def set_github_issue_url(conn: psycopg.Connection, fingerprint: str, github_issue_url: str) -> None:
    conn.execute(
        "UPDATE auto_error_reports SET github_issue_url = %s WHERE fingerprint = %s",
        (github_issue_url, fingerprint),
    )
