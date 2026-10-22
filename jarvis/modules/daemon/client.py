"""Client side of the local server daemon (design doc §2/§6/§10).

`ensure_daemon_running()` is what any client (today: the MCP-server
proxy and the CLI ingest entrypoints; later: the desktop app) calls
before talking to the daemon: reuse one that's already up, or start one
(design doc §6.1/§6.2/§6.4 - auto-start, no separate install step, no
manual "start the daemon" command required).

`call()` is the thin HTTP-call helper every typed wrapper below uses.
The typed wrappers (`search`, `get_hierarchy`, `browse`, `related`,
`query_db`, `run_ingest_filesystem`, `run_ingest_drive`, `register_client`,
`request_shutdown`) are what actual callers use - `jarvis/cmd/mcp_server.py`'s
tool proxies and the CLI ingest entrypoints both call these instead of
building request dicts themselves, so the request shape and the
daemon-unreachable handling below live in exactly one place.

Daemon-unreachable error handling (design doc §10 - three tiers):
  1. DaemonStartupError - auto-start itself failed (the daemon's process
     couldn't be launched, or launched but never answered /health within
     the timeout).
  2. DaemonConnectionError - a request to a daemon that was reachable
     moments ago (per the discovery file / a prior health check) failed
     outright - connection refused/reset, it responded with something
     that isn't valid JSON, or it responded with a non-2xx status that
     isn't one of our own `{"error": {"code", "message"}}` envelopes
     (`_is_error_envelope` - a real anomaly, not data any caller should
     be handed as if it were a normal response; see `_do_request`).
  3. DaemonTimeoutError - same situation, but the failure was specifically
     a timeout (the daemon accepted the connection but didn't respond in
     time) - kept distinct from #2 because "refused" and "hung" are
     different real symptoms even though a user can't act on either
     differently (see the identical message below).

Every real HTTP request (not the auto-start polling loop, which already
has its own longer poll/timeout) gets exactly one automatic retry after
a short pause before either of the last two tiers is raised - a cheap,
narrow mitigation for the daemon dying in the split second between a
health check and the real request, not a retry loop. The one exception:
a **timeout** on a write call (`run_ingest_filesystem`/`run_ingest_drive`,
via `retry_on_timeout=False` - design doc §7, open question #3, now
resolved) skips that retry, since the daemon may still be genuinely
working on the original request behind its write lock, and retrying
would fire a redundant second one at it. A connection failure always
still retries regardless of call kind, since it means the daemon never
even started the request.

The read-tool wrappers (search/get_hierarchy/browse/related/query_db)
catch all three tiers internally and return an `ErrorResponse` - the
existing MCP wire contract already returns errors as data, not raised
exceptions (see sql_query.py's docstring on why: a raised exception loses
the code/message distinction by the time an MCP client sees it). The two
ingest wrappers let the exception propagate instead - callers (today:
only the CLI) already have an exception-based error convention for
ingest, and there's no established "error variant" of a plain stats
dict to invent one for.
"""

import json
import logging
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, Optional, Union

import requests

from jarvis_server.daemon.protocol import API_VERSION
from jarvis_server.internal.logging.logging_config import default_log_dir, get_logger, rotate_if_large
from jarvis_server.mcp_tools.models import (
    BrowseResult,
    DocumentResult,
    ErrorDetail,
    ErrorResponse,
    FileTypeStatsResult,
    HierarchyResult,
    QueryResult,
    RelatedResult,
    SearchFilters,
    SearchResult,
    SyncRootsResult,
    ToolCallHistoryResult,
)

logger = get_logger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[3]
# JARVIS_DAEMON_HOME override: see jarvis_server.daemon.server's copy of
# this constant - must agree with it, since this is the file a client
# reads to find the daemon that constant's process wrote it to.
_DAEMON_HOME = Path(os.environ.get("JARVIS_DAEMON_HOME") or REPO_ROOT)
# A fresh packaged install has never created this directory - only the
# daemon itself does, once it actually starts (jarvis_server.daemon.
# server's own DEFAULT_DB_PATH.parent.mkdir call) - too late for the
# lock/log/discovery files below, which the client needs before a daemon
# has ever run even once. Confirmed directly: a real first-ever `--mcp`
# launch against an empty JARVIS_DAEMON_HOME crashes in
# _acquire_spawn_lock() with FileNotFoundError without this.
_DAEMON_HOME.mkdir(parents=True, exist_ok=True)
DISCOVERY_FILE = _DAEMON_HOME / ".jarvis_daemon.json"
_LOCK_FILE = _DAEMON_HOME / ".jarvis_daemon.lock"
# Unlike DISCOVERY_FILE/_LOCK_FILE above, this is a file a user (or a
# support conversation) is meant to actually go find, so it lives in the
# platform's own log location rather than app data.
LOG_FILE = default_log_dir() / "jarvis_daemon.log"
rotate_if_large(LOG_FILE)

_HEALTH_TIMEOUT_S = 0.5
# A cold start has to run every pending migration check and a full DB
# preflight (confirmed directly: a real cold start against a 318-chunk
# database took ~33s) before /health ever answers - 15s tripped this
# false on a database that wasn't even unusually large. 45s gives a
# real margin above the observed worst case without masking a genuinely
# dead daemon for too long.
_STARTUP_TIMEOUT_S = 45.0
_POLL_INTERVAL_S = 0.2
# A lock file older than this is assumed to be left over from a spawn
# that crashed before cleaning up, not a real in-progress start. Must
# stay comfortably above _STARTUP_TIMEOUT_S (see ensure_daemon_running's
# own comment on this) - a 15s margin, same as before the timeout bump.
_STALE_LOCK_S = 60.0
# The one automatic retry's pause (design doc §10) - long enough to
# outlast a brief hiccup, short enough nobody notices a UI hang.
_RETRY_DELAY_S = 1.5

# call()'s default timeout - every read (search/get_hierarchy/browse/
# related/query_db) uses this. Reads are expected to be fast; a timeout
# here is a real anomaly worth the one automatic retry below.
_DEFAULT_TIMEOUT_S = 120.0
# How often the ingest wrappers check whether their run has finished. A
# run has no time limit of its own: organize alone can take hours, and
# the daemon reports a dead run as unreachable on the next check anyway.
_INGEST_POLL_INTERVAL_S = 1.0
# How long a running ingest's daemon may go without answering a status
# check before the wrapper gives up on it - generous, because a slow
# answer during heavy organize work is normal and only silence this long isn't.
_INGEST_UNRESPONSIVE_S = 5 * 60.0
# request_shutdown()'s own timeout - deliberately short, not the 120s
# read default. It's called synchronously from MainWindow.closeEvent()
# (see main_window.py), which must never let the window hang for long
# because the daemon happens to be slow/unresponsive; a bounded few
# seconds is an acceptable worst-case pause on close, an unbounded one
# isn't. retry_on_timeout=False for the same reason a slow response
# here isn't worth doubling the wait for.
_SHUTDOWN_TIMEOUT_S = 3.0
# get_ingest_status()'s own timeout - this is polled repeatedly (design
# doc §4c) while a client's own long-running ingest call is already in
# flight, so a slow/stuck response here should give up fast and let the
# next poll tick try again, not compound a client-side hang on top of
# whatever's already happening with the real ingest request.
_INGEST_STATUS_TIMEOUT_S = 3.0

# jarvis_server.internal.logging.logging_config only configures stderr
# (logging.basicConfig with no filename=) - fine for a terminal, useless
# in a packaged app with no visible console. So every process that talks
# to the daemon (desktop app, MCP proxy, CLI) also logs - all of its
# loggers, via the root logger - to the same file the daemon subprocess's
# own stdout/stderr goes to (_spawn_daemon below): one file to check, not
# two. Never lets a logging-setup problem (e.g. an unwritable directory)
# break importing this module - stderr-only logging is a degraded
# fallback, not a reason to crash every daemon client.
_root_logger = logging.getLogger()
if not any(isinstance(h, logging.FileHandler) for h in _root_logger.handlers):
    try:
        _file_handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
        _file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        _root_logger.addHandler(_file_handler)
    except OSError:
        logger.warning("Could not open %s for daemon-client diagnostics - logging to stderr only", LOG_FILE)


class DaemonError(Exception):
    """Base for every daemon-communication failure. Never raised
    directly - always one of the three tiers below."""


class DaemonStartupError(DaemonError, RuntimeError):
    """Tier 1: the daemon could not be launched, or launched but never
    answered /health within the startup timeout. Also a RuntimeError so
    existing `except RuntimeError` call sites keep working unchanged."""


class DaemonConnectionError(DaemonError):
    """Tier 2: a request to a (believed-reachable) daemon failed outright
    - connection refused/reset, or a non-JSON response - even after the
    one retry."""


class DaemonTimeoutError(DaemonError):
    """Tier 3: same situation as DaemonConnectionError, but the failure
    was specifically a timeout, even after the one retry."""


_USER_MESSAGES = {
    DaemonStartupError: "The Jarvis background service didn't start in time. Try restarting the app.",
    DaemonConnectionError: "The Jarvis background service isn't responding. Try restarting the app.",
    DaemonTimeoutError: "The Jarvis background service isn't responding. Try restarting the app.",
}


def _read_discovery() -> Optional[dict]:
    try:
        return json.loads(DISCOVERY_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def _health_body(port: int) -> Optional[dict]:
    try:
        resp = requests.get(f"http://127.0.0.1:{port}/health", timeout=_HEALTH_TIMEOUT_S)
        return resp.json() if resp.ok else None
    except requests.RequestException:
        return None


def _health_check(port: int) -> bool:
    body = _health_body(port)
    return bool(body and body.get("ok") is True)


def _build_fingerprint() -> Optional[list]:
    """(size, mtime_ns) of `sys.executable` - the same on-disk file for
    both this process and any daemon it spawns (`_spawn_daemon`'s frozen
    branch), so it changes exactly when a reinstall/update replaces the
    packaged binary. None outside a frozen build - dev runs the daemon
    via `-m jarvis.cmd.daemon`, where "this install" isn't a single file
    that changes identity the same way, so there's nothing to compare."""
    if not getattr(sys, "frozen", False):
        return None
    try:
        stat = os.stat(sys.executable)
        return [stat.st_size, stat.st_mtime_ns]
    except OSError:
        return None


def _kill_stale_daemon(pid: Optional[int]) -> None:
    if pid is None:
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except (ProcessLookupError, OSError):
        pass


def _live_port() -> Optional[int]:
    info = _read_discovery()
    if not info:
        return None
    body = _health_body(info["port"])
    if not body or body.get("ok") is not True:
        return None

    fingerprint = _build_fingerprint()
    remote_fingerprint = body.get("build_fingerprint")
    stale_build = fingerprint is not None and remote_fingerprint not in (None, fingerprint)
    # Also catches a daemon too old to report a version at all, and a
    # dev client finding a packaged app's daemon (no fingerprint to compare).
    stale_api = (body.get("api_version") or 0) < API_VERSION
    if stale_build or stale_api:
        # A different build than this client is still serving - a
        # reinstall/update replaced the app while an old daemon kept
        # running. Never reuse it: killed directly rather than through
        # the ref-counted /shutdown route, since a stale build being
        # served is a correctness problem no other client should get a
        # vote on.
        logger.info(
            "jarvis daemon on port %s is outdated (pid %s, api_version %s) - killing it",
            info["port"], body.get("pid"), body.get("api_version"),
        )
        _kill_stale_daemon(body.get("pid", info.get("pid")))
        return None

    return info["port"]


def is_daemon_reachable() -> bool:
    """A passive, side-effect-free (aside from `_live_port`'s own stale-
    build cleanup) check - never spawns. For a caller that already
    showed "unreachable" and wants to notice a quiet recovery later
    (the GUI's own periodic recheck) without repeatedly attempting to
    spawn a new daemon on every tick the way calling `ensure_daemon_
    running()` again would."""
    return _live_port() is not None


def get_daemon_pid() -> Optional[int]:
    """The running daemon's PID, for local `psutil` introspection
    (RAM/uptime/force-kill - see `process_stats.py`), or None if no
    daemon is currently reachable. Reuses `_live_port`'s own liveness/
    stale-build check rather than re-parsing the discovery file."""
    port = _live_port()
    if port is None:
        return None
    body = _health_body(port)
    return body.get("pid") if body else None


def _acquire_spawn_lock() -> bool:
    """Best-effort mutual exclusion so two clients that both see "no
    daemon" don't each spawn one (design doc: not solved in full - see
    the caveat in local-server-architecture-DESIGN.md's implementation
    notes). Atomic create-if-absent; a stale lock (crashed spawner) is
    reclaimed after `_STALE_LOCK_S`."""
    try:
        fd = os.open(_LOCK_FILE, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.close(fd)
        return True
    except FileExistsError:
        try:
            if time.time() - _LOCK_FILE.stat().st_mtime > _STALE_LOCK_S:
                _LOCK_FILE.unlink(missing_ok=True)
                return _acquire_spawn_lock()
        except OSError:
            pass
        return False


def _release_spawn_lock() -> None:
    _LOCK_FILE.unlink(missing_ok=True)


def _spawn_daemon() -> None:
    kwargs: dict[str, Any] = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    try:
        log_fh = open(LOG_FILE, "a", encoding="utf-8")
        # PYTHONHASHSEED must be set before the interpreter boots (see
        # jarvis/cmd/daemon.py's module docstring for why this matters
        # and how it was verified) - setting it here means the common
        # auto-start path is born already-seeded and never has to take
        # daemon.py's own relaunch fallback at all.
        env = dict(os.environ)
        env["PYTHONHASHSEED"] = "0"
        # sys.frozen (PyInstaller's own standard flag, only ever present
        # on a frozen executable's sys module) - design doc §4, resolved:
        # a packaged desktop app is one frozen binary, argv-dispatched.
        # `sys.executable` there IS that binary, not a general Python
        # interpreter capable of `-m <module>` - confirmed directly this
        # doesn't work in a frozen build (`-m jarvis.cmd.daemon` would
        # just relaunch the same frozen GUI a second time, unhandled).
        # `--daemon` is caught by jarvis/cmd/desktop_app.py's own argv
        # dispatch, which runs jarvis_server.daemon.server.main() -
        # the same thing `-m jarvis.cmd.daemon` runs today, unfrozen.
        if getattr(sys, "frozen", False):
            argv = [sys.executable, "--daemon"]
        else:
            argv = [sys.executable, "-m", "jarvis.cmd.daemon"]
        subprocess.Popen(
            argv,
            cwd=str(REPO_ROOT),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=log_fh,
            stderr=log_fh,
            **kwargs,
        )
    except OSError as exc:
        logger.exception("Could not launch the Jarvis daemon process")
        raise DaemonStartupError(_USER_MESSAGES[DaemonStartupError]) from exc


def ensure_daemon_running(startup_timeout: float = _STARTUP_TIMEOUT_S) -> int:
    """Return the port of a live Jarvis daemon, starting one first if
    none is currently reachable. Safe to call from any client - reused
    for every request, not just once at client startup, so a daemon that
    died mid-session gets restarted on the next call rather than leaving
    the client stuck.

    Raises DaemonStartupError (tier 1) if no daemon becomes reachable
    within `startup_timeout` - this is the auto-start poll loop itself,
    separate from and not covered by the one-retry behavior in `call()`
    below (that's for a single request against an already-confirmed
    daemon, not "start one from nothing")."""
    port = _live_port()
    if port is not None:
        return port

    deadline = time.monotonic() + startup_timeout
    if _acquire_spawn_lock():
        try:
            # Re-check: the daemon may have come up between the first
            # _live_port() call and winning the lock.
            port = _live_port()
            if port is None:
                logger.info("jarvis daemon not reachable - starting one")
                _spawn_daemon()
            # Hold the lock until our own spawn is confirmed healthy (or
            # we time out) - releasing right after Popen() returns would
            # let a second client, still seeing no live port, spawn a
            # duplicate before ours finishes starting up. _STALE_LOCK_S
            # (30s) exceeds startup_timeout (15s default) so this hold
            # is never itself mistaken for a crashed spawner mid-poll.
            while port is None and time.monotonic() < deadline:
                time.sleep(_POLL_INTERVAL_S)
                port = _live_port()
        finally:
            _release_spawn_lock()
        if port is not None:
            return port
    else:
        # Another client is already spawning one and holds the lock -
        # just poll for it to come up rather than also spawning.
        while time.monotonic() < deadline:
            port = _live_port()
            if port is not None:
                return port
            time.sleep(_POLL_INTERVAL_S)

    logger.error(
        "Jarvis daemon did not become reachable within %.1fs - see %s", startup_timeout, LOG_FILE,
    )
    raise DaemonStartupError(_USER_MESSAGES[DaemonStartupError])


def _is_error_envelope(body: Any) -> bool:
    """True if `body` is a deliberate `{"error": {"code", "message"}}`
    response - the MCP wire format `ErrorResponse.model_dump()` produces
    (see server.py's `_model_response` docstring) and the exact shape the
    two ingest routes build by hand in their except blocks. Any caller
    that gets one of these already knows to check for it (every read
    wrapper via `_read_call`'s `"error" in body`, both ingest wrappers'
    callers per their own docstrings) - it's a real, understood error,
    just carried as data instead of a raised exception, by design."""
    return (
        isinstance(body, dict)
        and isinstance(body.get("error"), dict)
        and "code" in body["error"]
        and "message" in body["error"]
    )


def _do_request(method: str, url: str, json_body: Optional[dict], timeout: float) -> dict:
    resp = requests.request(method, url, json=json_body, timeout=timeout)
    body = resp.json()  # a non-JSON body raises ValueError here, inside the retry logic below
    if not (200 <= resp.status_code < 300) and not _is_error_envelope(body):
        # A non-2xx response that ISN'T one of our own deliberate error
        # envelopes is a real anomaly - previously this was handed back
        # to the caller as if it were ordinary successful data, silently
        # relying on every current AND future caller happening to check
        # for an "error" key that might not even be there (a bare
        # Starlette 404/validation-error body, a proxy's error page, a
        # bug that returns some other shape entirely). Raising here
        # routes it through the exact same retry-then-tier machinery as
        # a malformed/non-JSON body below, instead of letting it slip
        # through as if nothing were wrong.
        raise ValueError(f"daemon returned HTTP {resp.status_code} with an unexpected body: {body!r}")
    return body


def call(
    method: str,
    path: str,
    json_body: Optional[dict] = None,
    timeout: float = _DEFAULT_TIMEOUT_S,
    retry_on_timeout: bool = True,
) -> dict:
    """Make one request against the daemon, auto-starting it first if
    needed (tier 1 - propagates DaemonStartupError untouched).

    A connection failure always gets exactly one retry, after
    `_RETRY_DELAY_S` - not a retry loop - before raising
    DaemonConnectionError (tier 2). A refused/reset connection means the
    daemon never even started processing this request, so retrying it is
    always safe.

    A **timeout** retries the same way only when `retry_on_timeout` is
    True (the default, used by every read wrapper below). When it's
    False (design doc §7, open question #3 - now resolved; the two
    ingest wrappers pass this), a timeout raises DaemonTimeoutError (tier
    3) immediately, with no retry. This is deliberate, not an oversight:
    unlike a connection failure, a timeout on a write means the daemon
    may still have acted on it - starting an ingest is serialized behind
    the daemon's own write lock. Retrying it would fire a second,
    redundant ingest request at a daemon that's already busy with the
    first one - confirmed as a real, reproduced problem during manual
    desktop-app testing, not a hypothetical.

    Returns the parsed JSON body regardless of status code on success -
    an error response from the daemon itself is still a JSON dict
    (design doc: the daemon reuses the MCP wire format,
    `{"error": {"code", "message"}}` on failure) for the caller to
    branch on, same as calling the underlying function in-process would
    have returned. A daemon-unreachable failure (this function raising)
    is a different, unrelated case from the daemon *responding* with an
    application-level error."""
    port = ensure_daemon_running()
    url = f"http://127.0.0.1:{port}{path}"

    try:
        return _do_request(method, url, json_body, timeout)
    except requests.exceptions.Timeout as exc:
        if not retry_on_timeout:
            logger.exception(
                "daemon request timed out (%s %s) - not retrying (write call, may still "
                "be in progress server-side)", method, path,
            )
            raise DaemonTimeoutError(_USER_MESSAGES[DaemonTimeoutError]) from exc
        logger.warning("daemon request timed out (%s %s) - retrying once", method, path)
        time.sleep(_RETRY_DELAY_S)
        try:
            return _do_request(method, url, json_body, timeout)
        except requests.exceptions.Timeout as exc2:
            logger.exception("daemon request timed out again after one retry (%s %s)", method, path)
            raise DaemonTimeoutError(_USER_MESSAGES[DaemonTimeoutError]) from exc2
        except (requests.exceptions.RequestException, ValueError) as exc2:
            logger.exception("daemon request failed on retry (%s %s)", method, path)
            raise DaemonConnectionError(_USER_MESSAGES[DaemonConnectionError]) from exc2
    except (requests.exceptions.RequestException, ValueError):
        logger.warning("daemon request failed (%s %s) - retrying once", method, path)
        time.sleep(_RETRY_DELAY_S)
        try:
            return _do_request(method, url, json_body, timeout)
        except requests.exceptions.Timeout as exc:
            logger.exception("daemon request timed out on retry (%s %s)", method, path)
            raise DaemonTimeoutError(_USER_MESSAGES[DaemonTimeoutError]) from exc
        except (requests.exceptions.RequestException, ValueError) as exc:
            logger.exception("daemon request failed again after one retry (%s %s)", method, path)
            raise DaemonConnectionError(_USER_MESSAGES[DaemonConnectionError]) from exc


# ---------------------------------------------------------------------
# Typed wrappers. Read tools: catch DaemonError, return ErrorResponse -
# matches the existing MCP wire contract (see module docstring). Ingest:
# let DaemonError propagate - the CLI's existing exception-based error
# handling already covers it, and a plain stats dict has no error shape
# to invent one for.
# ---------------------------------------------------------------------


def _read_call(fn: Callable[[], dict], success_model) -> Any:
    try:
        body = fn()
    except DaemonError as exc:
        code = "daemon_timeout" if isinstance(exc, DaemonTimeoutError) else "daemon_unavailable"
        return ErrorResponse(error=ErrorDetail(code=code, message=str(exc)))
    if "error" in body:
        return ErrorResponse.model_validate(body)
    return success_model.model_validate(body)


def search(
    query: str,
    filters: Optional[SearchFilters] = None,
    limit: int = 10,
    cursor: Optional[str] = None,
) -> Union[SearchResult, ErrorResponse]:
    return _read_call(
        lambda: call("POST", "/search", {
            "query": query,
            "filters": filters.model_dump(mode="json") if filters is not None else None,
            "limit": limit,
            "cursor": cursor,
        }),
        SearchResult,
    )


def get_hierarchy(
    root: Optional[str] = None, depth: Optional[int] = None
) -> Union[HierarchyResult, ErrorResponse]:
    return _read_call(
        lambda: call("POST", "/get_hierarchy", {"root": root, "depth": depth}),
        HierarchyResult,
    )


def browse(
    topic_id: str, cursor: Optional[str] = None, limit: Optional[int] = None
) -> Union[BrowseResult, ErrorResponse]:
    return _read_call(
        lambda: call("POST", "/browse", {"topic_id": topic_id, "cursor": cursor, "limit": limit}),
        BrowseResult,
    )


def related(id: str, limit: Optional[int] = None) -> Union[RelatedResult, ErrorResponse]:
    return _read_call(
        lambda: call("POST", "/related", {"id": id, "limit": limit}),
        RelatedResult,
    )


def get_document(
    document_id: Optional[str] = None, title: Optional[str] = None
) -> Union[DocumentResult, ErrorResponse]:
    return _read_call(
        lambda: call("POST", "/get_document", {"document_id": document_id, "title": title}),
        DocumentResult,
    )


def query_db(sql: str, limit: int = 50) -> Union[QueryResult, ErrorResponse]:
    return _read_call(
        lambda: call("POST", "/query_db", {"sql": sql, "limit": limit}),
        QueryResult,
    )


def get_file_type_stats() -> Union[FileTypeStatsResult, ErrorResponse]:
    return _read_call(
        lambda: call("GET", "/stats/file_types"),
        FileTypeStatsResult,
    )


def get_sync_roots() -> Union[SyncRootsResult, ErrorResponse]:
    return _read_call(
        lambda: call("GET", "/sync_roots"),
        SyncRootsResult,
    )


def get_tool_call_history(
    cursor: Optional[int] = None, limit: int = 50
) -> Union[ToolCallHistoryResult, ErrorResponse]:
    """`cursor` is the `before_id` pagination cursor - the smallest `id`
    already seen, to fetch the next older page (see `get_recent_tool_calls`).
    A GET route, unlike the POST reads above - there's no request body,
    so `call()`'s `json_body` stays None and the params ride the URL."""
    path = f"/stats/tool_calls?limit={limit}"
    if cursor is not None:
        path += f"&before_id={cursor}"
    return _read_call(lambda: call("GET", path), ToolCallHistoryResult)


def remove_sync_root(connector: str, root_id: str) -> dict:
    """Take one source out of the library - `{"removed": <file count>}`,
    or an `"error"` dict (a sync is running, Drive needs signing in
    again, ...), same contract as the ingest wrappers below. Safe to
    retry: removing an already-removed source changes nothing."""
    return call("POST", "/sync_roots/remove", {"connector": connector, "root_id": root_id})


def run_ingest_filesystem(path: str, organize_hierarchy: bool = True) -> dict:
    """Ingest one local folder via the daemon, blocking until the run ends.
    Raises DaemonError (one of the three tiers) if the daemon can't be
    reached at all; a daemon-side ingest failure (a bad path, a bad
    EMBEDDING_API_KEY, ...) comes back as a plain dict with an `"error"`
    key instead - callers that need to tell the two apart check for that
    key, the same distinction `filesystem_ingest.py` already made when it
    called `run_ingest()` in-process and could get either a result or a
    raised exception."""
    return _run_ingest("/ingest/filesystem", {"path": path, "organize_hierarchy": organize_hierarchy})


def run_ingest_drive(folder_id: str, organize_hierarchy: bool = True) -> dict:
    """Ingest one Drive folder via the daemon. Same contract as
    `run_ingest_filesystem`."""
    return _run_ingest("/ingest/drive", {"folder_id": folder_id, "organize_hierarchy": organize_hierarchy})


def _run_ingest(path: str, body: dict) -> dict:
    """The daemon starts the run and replies at once; this waits for the
    run's outcome on /ingest/status. Starting it is never retried on a
    timeout: the daemon may have started the run anyway, and a second
    request would only be refused as `ingest_in_progress`."""
    started = call("POST", path, body, retry_on_timeout=False)
    if "error" in started:
        return started
    # Polled directly on the daemon running this ingest, not via call():
    # call()'s auto-start treats a slow /health as a dead daemon and would
    # spawn a second one while this one is busy organizing.
    info = _read_discovery()
    if not info:
        raise DaemonConnectionError(_USER_MESSAGES[DaemonConnectionError])
    status_url = f"http://127.0.0.1:{info['port']}/ingest/status"
    last_answer = time.monotonic()
    while True:
        time.sleep(_INGEST_POLL_INTERVAL_S)
        try:
            status = _do_request("GET", status_url, None, _INGEST_STATUS_TIMEOUT_S)
        except requests.exceptions.Timeout as exc:
            # Busy, not gone - keep waiting unless it stays silent too long.
            if time.monotonic() - last_answer > _INGEST_UNRESPONSIVE_S:
                logger.exception("daemon stopped answering /ingest/status mid-ingest")
                raise DaemonTimeoutError(_USER_MESSAGES[DaemonTimeoutError]) from exc
            continue
        except (requests.exceptions.RequestException, ValueError) as exc:
            logger.exception("daemon became unreachable mid-ingest")
            raise DaemonConnectionError(_USER_MESSAGES[DaemonConnectionError]) from exc
        last_answer = time.monotonic()
        if not status["in_progress"] and status.get("outcome") is not None:
            return status["outcome"]


def get_ingest_status() -> dict:
    """Poll the daemon's current ingest phase (design doc §4c) -
    `{"phase": ..., "batch_progress": {...} | None, "in_progress": bool}`.
    `batch_progress`, when not None, is
    `{"current_batch", "total_batches", "processed_items", "total_items"}`
    for the embed phase specifically. Meant to be called on a timer while
    one of the two ingest wrappers above is already in-flight against the
    same daemon, to surface live phase text and progress counts a client
    otherwise has no way to see (one HTTP call runs the whole pipeline,
    no streaming response). Raises DaemonError like every other typed
    wrapper here; a caller polling this on a timer should treat that as
    "skip this tick" - the ingest call's own result is what actually
    determines success or failure, not this.

    `cancel_requested` reflects a `cancel_ingest()` call that landed but
    hasn't taken effect yet - see that function's own docstring."""
    return call("GET", "/ingest/status", timeout=_INGEST_STATUS_TIMEOUT_S, retry_on_timeout=False)


def cancel_ingest() -> dict:
    """Request cancellation of whatever ingest is currently in flight -
    `{"cancel_requested": bool}`, plus a `"reason"` key when False (no
    ingest was in flight to cancel). Cooperative, not instant: the
    in-flight ingest call only checks this at its own between-phase/
    between-batch checkpoints, so a caller shouldn't treat a True
    response as "the ingest has stopped" - the ingest call's own eventual
    response (`{"cancelled": true, ...}`) is what confirms that. Uses
    `_INGEST_STATUS_TIMEOUT_S`, same as `get_ingest_status` - this is a
    quick flag flip, not a call that does the actual work."""
    return call(
        "POST", "/ingest/cancel", timeout=_INGEST_STATUS_TIMEOUT_S, retry_on_timeout=False,
    )


# ---------------------------------------------------------------------
# Client registration + shutdown - the fix for a real, confirmed bug:
# closing the GUI used to always orphan its own spawned daemon (see
# jarvis_server.daemon.server's module docstring for the full mechanism
# and why an OS-level process-tree kill could never be the fix here).
# Like the ingest wrappers, both let DaemonError propagate rather than
# returning an ErrorResponse - callers here (MainWindow, mcp_server.py)
# are lifecycle hooks, not the read-tool wire contract sql_query.py's
# docstring explains, and both already wrap these calls to make a
# failure here a no-op, never a crash.
# ---------------------------------------------------------------------


def register_client() -> str:
    """Register this process as a daemon client, so the daemon knows not
    to shut down on someone else's request while this one's still
    around. Callers that register opportunistically (on daemon-ready,
    not on every request) should treat a raised DaemonError as fine to
    ignore - it just means this client won't be able to ask for a clean
    shutdown later, not that anything else stops working."""
    body = call("POST", "/clients/register", {})
    return body["client_id"]


def request_shutdown(client_id: str) -> dict:
    """Retire `client_id` and ask the daemon to shut down if it was the
    last one relying on it (see jarvis_server.daemon.server.api_shutdown
    for the actual decision). Short, non-retried timeout - see
    _SHUTDOWN_TIMEOUT_S."""
    return call(
        "POST", "/shutdown", {"client_id": client_id},
        timeout=_SHUTDOWN_TIMEOUT_S, retry_on_timeout=False,
    )
