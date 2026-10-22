"""Local process/storage introspection for the Settings page - inspects
PIDs and paths this process can already resolve locally (no daemon HTTP
round-trip needed for any of this)."""

import time

import psutil

from jarvis.modules.daemon import client as daemon_client
from jarvis_server.internal.db.connection import DEFAULT_DB_PATH
from jarvis_server.internal.graph_db.graph_db import DEFAULT_GRAPH_PATH


def get_daemon_pid() -> int | None:
    return daemon_client.get_daemon_pid()


def get_process_ram(pid: int) -> int | None:
    try:
        return psutil.Process(pid).memory_info().rss
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return None


def get_daemon_uptime_seconds(pid: int) -> float | None:
    try:
        return time.time() - psutil.Process(pid).create_time()
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return None


def force_kill_daemon(pid: int) -> None:
    """SIGKILL the daemon process. Does not respawn it - the caller
    (desktop app) does that afterward via the existing
    `ensure_daemon_running()` flow, same as any other daemon-died case."""
    try:
        psutil.Process(pid).kill()
    except psutil.NoSuchProcess:
        pass


def force_restart_daemon() -> None:
    """Kill the daemon outright, then bring a fresh one up through the
    ordinary auto-start - the fresh one picks up settings saved since the
    old one started (a new API key, for one). Blocking: call it from a
    worker thread."""
    pid = get_daemon_pid()
    if pid is not None:
        force_kill_daemon(pid)
    daemon_client.ensure_daemon_running()


def get_storage_usage() -> dict:
    db_bytes = DEFAULT_DB_PATH.stat().st_size if DEFAULT_DB_PATH.exists() else 0

    # DEFAULT_GRAPH_PATH is a single file, not a directory: graph_db.py's
    # get_connection() passes it straight to `lb.Database(str(graph_path))`,
    # and staged_build.py's atomic swap does `os.replace(staging_path,
    # live_graph_path)` - a single-path rename. Confirmed empirically too:
    # the repo's own jarvis_graph.lbdb is a plain ~7MB regular file.
    graph_bytes = DEFAULT_GRAPH_PATH.stat().st_size if DEFAULT_GRAPH_PATH.exists() else 0

    return {
        "db_bytes": db_bytes,
        "graph_bytes": graph_bytes,
        "total_bytes": db_bytes + graph_bytes,
    }
