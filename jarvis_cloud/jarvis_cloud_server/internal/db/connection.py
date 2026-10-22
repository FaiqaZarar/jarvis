"""Connection lifecycle for the Postgres store.

Run `python -m jarvis_cloud_server.internal.migrate` first to create the
tables (or call `run_migrations()` directly, as `server.py`'s startup
does).

Unlike jarvis_server's per-user SQLite connection (one file, one
process), this is a shared, multi-user server - concurrent requests need
their own connections, not one connection reused across the process. A
pool (opened once, at import time) is the DB-agnostic equivalent of that
module's single `sqlite3.connect()` call.
"""

import os

from psycopg_pool import ConnectionPool

from jarvis_cloud_server.internal.logging.logging_config import get_logger

logger = get_logger(__name__)


def _default_dsn() -> str:
    dsn = os.environ.get("JARVIS_CLOUD_DATABASE_URL")
    if not dsn:
        raise RuntimeError(
            "JARVIS_CLOUD_DATABASE_URL is not set. Example: "
            "postgresql://jarvis_cloud:<password>@127.0.0.1:5432/jarvis_cloud_dev"
        )
    return dsn


# Opened lazily, not at import time: importing this module (e.g. from a
# test that immediately monkeypatches get_pool) must not require a real
# Postgres connection to already be reachable.
_pool: ConnectionPool | None = None


def get_pool() -> ConnectionPool:
    global _pool
    if _pool is None:
        _pool = ConnectionPool(conninfo=_default_dsn(), min_size=1, max_size=10, open=True)
    return _pool


def get_connection():
    """A single connection checked out from the pool, as a context
    manager (`with get_connection() as conn:`) - psycopg_pool returns the
    connection to the pool on exit rather than closing it, matching the
    "one connection per request" shape route handlers need."""
    try:
        return get_pool().connection()
    except Exception:
        logger.exception("Failed to check out a database connection from the pool")
        raise
