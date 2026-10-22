"""Shared error-handling/commit ritual for the table modules in this
package.

Every table module wraps its reads with `@read_query` and its writes with
`@write_query` instead of hand-rolling `try/except sqlite3.Error: ...`
itself - see docs/coding-guides/database-access.md for the convention
this replaces.
"""

import functools
import sqlite3

from jarvis_server.internal.logging.logging_config import get_logger

logger = get_logger(__name__)


def read_query(func):
    """Wrap a function whose body only reads: log + re-raise on
    sqlite3.Error. Never commits/rolls back - nothing was written."""

    @functools.wraps(func)
    def wrapper(conn: sqlite3.Connection, *args, **kwargs):
        try:
            return func(conn, *args, **kwargs)
        except sqlite3.Error:
            logger.exception("%s failed args=%r kwargs=%r", func.__name__, args, kwargs)
            raise

    return wrapper


def write_query(func):
    """Wrap a function whose body writes: commit on success, rollback +
    log + re-raise on sqlite3.Error.

    Every wrapped function gains a `commit: bool = True` keyword-only
    argument, even though its own `def` doesn't declare one - pass
    `commit=False` to fold several write_query calls into one atomic
    commit at the caller (see embed_and_index_chunks for the pattern:
    several no-commit writes, then one explicit `conn.commit()`). A
    failure partway through such a batch still rolls back everything
    since the last real commit, because nothing in the batch was ever
    committed."""

    @functools.wraps(func)
    def wrapper(conn: sqlite3.Connection, *args, commit: bool = True, **kwargs):
        try:
            result = func(conn, *args, **kwargs)
            if commit:
                conn.commit()
            return result
        except sqlite3.Error:
            conn.rollback()
            logger.exception("%s failed args=%r kwargs=%r", func.__name__, args, kwargs)
            raise

    return wrapper
