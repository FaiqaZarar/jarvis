"""Connection lifecycle for the SQLite store.

Run `python -m jarvis.cmd.migrate` first to create the tables.
"""

import os
import sqlite3
from pathlib import Path

import sqlite_vec

from jarvis_server.internal.logging.logging_config import get_logger

logger = get_logger(__name__)

# JARVIS_DB_PATH override exists for one reason: a real daemon subprocess
# (jarvis/cmd/daemon.py), spawned fresh by a test, has no in-process
# object to monkeypatch - unlike every other test in this repo, which
# patches this module's get_connection directly. Without this, a
# subprocess-based daemon test would have no way to avoid touching the
# real <repo>/jarvis.db.
#
# JARVIS_DAEMON_HOME (see jarvis/modules/daemon/{client,server}.py's own
# copies of this constant) is a second, coarser override: when the
# desktop app relocates everything into one OS-standard app-data
# directory, setting that one variable should be enough - nobody should
# have to also set JARVIS_DB_PATH/JARVIS_GRAPH_PATH separately just to
# keep the db and the graph file next to it. JARVIS_DB_PATH still wins
# outright when it's set on its own (existing tests set it independently
# of JARVIS_DAEMON_HOME and must keep working unchanged) - this is a
# fallback for when only the coarser variable is set, not a replacement
# for the finer one.
def _default_db_path() -> Path:
    if os.environ.get("JARVIS_DB_PATH"):
        return Path(os.environ["JARVIS_DB_PATH"])
    if os.environ.get("JARVIS_DAEMON_HOME"):
        return Path(os.environ["JARVIS_DAEMON_HOME"]) / "jarvis.db"
    # Reaches the repo root - verified empirically, not just counted by
    # eye (Path(__file__).resolve().parents[5] == repo_root) - was
    # parents[3] before the server/client isolation move relocated this
    # file two levels deeper; fixed here, not left silently broken.
    return Path(__file__).resolve().parents[5] / "jarvis.db"


DEFAULT_DB_PATH = _default_db_path()

# Milliseconds SQLite will silently retry against a lock before raising
# `database is locked`, instead of the previous default (~0 - Python's
# own 5s sqlite3 timeout only covers its own retry loop, separate from
# SQLite's busy handler). Matters now that the local-server daemon
# (docs/local-server-architecture-DESIGN.md) can have more than one
# connection open at once - a concurrent reader and an in-flight write
# should queue briefly, not fail immediately.
_BUSY_TIMEOUT_MS = 5000


def get_connection(db_path: Path = DEFAULT_DB_PATH) -> sqlite3.Connection:
    """Open a connection with Row access by name, with sqlite-vec loaded,
    WAL journaling, and a busy timeout.

    No FK enforcement: relationships between tables are enforced at the
    application layer, not by the engine (see migrations/0001's header
    comment for why).

    sqlite-vec (J-06) is loaded on every connection, not just an
    embedding-specific one - vector operations (chunk_vectors) then work
    anywhere the rest of the app already opens a connection, rather than
    needing a second, inconsistent connection path.

    WAL mode + a busy timeout (local-server-architecture design, §1c/§4):
    previously neither was set, so a writer blocked every reader for the
    duration of its transaction, and a second connection that hit that
    lock failed immediately with `sqlite3.OperationalError: database is
    locked` rather than queuing. `PRAGMA journal_mode=WAL` is stored in
    the database file itself (set once, applies to every future
    connection - the call here is idempotent, not just first-run) and
    lets readers proceed against a snapshot while a write is in flight;
    the busy timeout then covers the remaining case SQLite's own
    single-writer rule still serializes - two writers overlapping - by
    waiting briefly instead of erroring outright.
    """
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        return conn
    except sqlite3.Error:
        logger.exception("Failed to open database connection db_path=%s", db_path)
        raise
