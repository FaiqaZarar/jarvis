"""Shared error-handling/commit ritual for the table modules in this
package.

Every table module wraps its reads with `@read_query` and its writes with
`@write_query` instead of hand-rolling `try/except psycopg.Error: ...`
itself - the same convention jarvis_server's own `internal/db/_middleware.py`
uses for SQLite (see docs/coding-guides/database-access.md), translated to
psycopg's exception type and commit/rollback calls.
"""

import functools

import psycopg

from jarvis_cloud_server.internal.logging.logging_config import get_logger

logger = get_logger(__name__)


def read_query(func):
    """Wrap a function whose body only reads: log + re-raise on
    psycopg.Error. Never commits/rolls back - nothing was written."""

    @functools.wraps(func)
    def wrapper(conn: psycopg.Connection, *args, **kwargs):
        try:
            return func(conn, *args, **kwargs)
        except psycopg.Error:
            logger.exception("%s failed args=%r kwargs=%r", func.__name__, args, kwargs)
            raise

    return wrapper


def write_query(func):
    """Wrap a function whose body writes: commit on success, rollback +
    log + re-raise on psycopg.Error.

    Every wrapped function gains a `commit: bool = True` keyword-only
    argument, even though its own `def` doesn't declare one - pass
    `commit=False` to fold several write_query calls into one atomic
    commit at the caller, same convention as jarvis_server's own
    write_query."""

    @functools.wraps(func)
    def wrapper(conn: psycopg.Connection, *args, commit: bool = True, **kwargs):
        try:
            result = func(conn, *args, **kwargs)
            if commit:
                conn.commit()
            return result
        except psycopg.Error:
            conn.rollback()
            logger.exception("%s failed args=%r kwargs=%r", func.__name__, args, kwargs)
            raise

    return wrapper
