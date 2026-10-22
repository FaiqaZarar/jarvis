"""Jarvis local server daemon.

The single local background process that owns `jarvis.db` and the
LadybugDB graph file (see `docs/local-server-architecture-DESIGN.md`).
The MCP-server proxy (`jarvis/cmd/mcp_server.py`) and, eventually, the
desktop app talk to this process over local HTTP instead of opening
either file themselves.

    python -m jarvis.cmd.daemon

Binds an OS-assigned port on `127.0.0.1` (design doc §6.3 - avoids a
fixed port colliding with anything else on the user's machine) and
writes `{"port": ..., "pid": ...}` to `DISCOVERY_FILE` so a client can
find it. `jarvis.modules.daemon.client.ensure_daemon_running` is the
client side, including auto-starting one if none is reachable
(design doc §6.1/§6.4).

Route handlers call the exact same functions the codebase already had -
`search()`, `get_hierarchy()`, `browse()`, `related()`, `query_db()`
from `jarvis_server.mcp_tools`, and `pipeline.run_ingest()` for the two
ingest routes - unchanged. Nothing here reimplements their logic; this
module is only the "who's allowed to call them, and how a request
reaches them" layer (design doc §4).

Concurrency (design doc §4, resolved decision #5): reads (search,
get_hierarchy, browse, related, query_db) are NOT serialized against
each other or against an in-flight write - each opens its own
connection exactly as it already did, and WAL mode
(`jarvis_server.internal.db.connection`) lets a reader proceed against a
consistent snapshot while a write is mid-transaction. Writes (ingest)
ARE serialized against each other via `_WRITE_LOCK`, because
`run_ingest`'s organize/reconcile step opens the LadybugDB graph file,
which allows exactly one writer, full stop - two ingests interleaved
wrongly inside this one process is the same hazard the whole daemon
exists to remove between processes.

Deliberately NOT "hold one shared `sqlite3.Connection` for the daemon's
whole lifetime": a single shared connection object would serialize
reads and writes together regardless of WAL, defeating the point of
enabling it. "One owner" here means one *process* ever opens `jarvis.db`
or the graph file, not one *connection object* for every request - see
the design doc's §4 note on this exact distinction.

Ingest phase tracking (docs/desktop-app-ux-redesign-DESIGN.md §4c): the
two ingest routes start `pipeline.run_ingest()` on its own thread and
reply 202 straight away - a run can take hours, far longer than an HTTP
request should be held open (a client timing out on a still-working run
was a real, reproduced failure; docs/observability-DESIGN.md §11).
`_ingest_status` is a plain module-level dict, updated via
`run_ingest`'s `on_phase` callback as the pipeline moves through its
stages, and holding the finished run's `outcome`; `GET /ingest/status`
reads it back. A single global is safe here specifically because
`_WRITE_LOCK` already guarantees at most one ingest is ever in flight
per daemon process - the same fact the concurrency note below and
docs/organize-checkpointing-DESIGN.md §1a already rely on. The phase
string itself is deliberately not a queryable DB row or a websocket/SSE
push - a client polls the one cheap GET route on a timer until the run
ends.

The embed phase's batch/item counts are ALSO persisted, in
`ingest_run_phases` (migration 0016) - written by `embed_and_index_chunks`
itself, committing once per batch (see that function's docstring), so
that table is a genuinely live, other-process-queryable view too (e.g.
via `query_db`), not just this route. This route's own `batch_progress`
field is still served from the in-memory `_ingest_status` rather than
reading the DB back - updated via the same `on_batch` callback, so it
tracks the DB row exactly, with no extra query per poll. Every other
phase gets only a start/end row in `ingest_run_phases` (no extra DB
write per item) - see the migration header. Every phase but reconcile
does report live item counts in memory, though: `progress` (via
`run_ingest`'s `on_progress`), reset on each phase change so one phase's
counts never render under the next phase's label. `ingest_runs`
(also migration 0016) is the per-run record this route has no need to
read: one row per `pipeline.run_ingest` call, keyed by the `run_id` its
result dict carries, outliving this process's in-memory `_ingest_status`
so a run's outcome (including `cancelled`/`interrupted`) survives past
the next ingest.

Client-registration + shutdown (real, confirmed bug fix): closing the
GUI used to always orphan its own spawned daemon, invisibly, forever -
`_spawn_daemon()` uses `DETACHED_PROCESS`/`start_new_session` specifically
so the daemon survives things like a browser-style "close the window"
without dying alongside a merely-backgrounded parent, which means there
is no OS-level process-tree kill that could ever safely be "the fix"
here - it would have no way to know whether some *other* client (the
CLI, or Claude Desktop's own `--mcp` connection) still needs this exact
daemon. The fix has to be an explicit, daemon-side decision instead:
`/clients/register` hands out an opaque id; `/shutdown` retires that id
and only actually stops the process once (a) no other registered client
remains AND (b) `_WRITE_LOCK` isn't currently held - the second check
protects a `--yes`-driven CLI ingest that never bothered to register at
all (registering for a short-lived, one-shot process was judged not
worth the added surface - see jarvis/cmd/filesystem_ingest.py/
drive_ingest.py, both still unregistered, deliberately), since an ingest
in flight takes seconds to minutes, not milliseconds, so this is real
protection for the realistic window, not just for a client that happened
to call `/clients/register`. The GUI (`MainWindow`) and `--mcp`
(`jarvis/cmd/mcp_server.py`) both register once when they first confirm
the daemon is reachable, and both request their own retirement on a
clean exit - whichever one is *last* is the one whose shutdown request
actually takes effect. A client that vanishes without unregistering
(forcefully killed, crashed) leaves a phantom registration that can keep
a truly-idle daemon alive indefinitely - a real, accepted limitation
(a different, lesser failure mode than the orphan bug this closes, which
happened on every clean close, not just some ungraceful ones) that a
full heartbeat/TTL scheme would close but wasn't judged necessary here.
"""

import asyncio
import functools
import json
import os
import socket
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from jarvis_server.internal.env_config import load_env

# Reaches the repo root - verified empirically
# (Path(__file__).resolve().parents[4] == repo_root) - was parents[3]
# before the server/client isolation move relocated this file one level
# deeper; fixed here, not left silently broken.
REPO_ROOT = Path(__file__).resolve().parents[4]
# JARVIS_DAEMON_HOME override: same rationale as JARVIS_DB_PATH in
# jarvis/internal/db/connection.py - lets a real subprocess-spawned
# daemon (in tests) write its discovery file somewhere other than the
# real repo root. Must agree with jarvis.modules.daemon.client's copy of
# this - a client and the daemon it's talking to need the same file.
_DAEMON_HOME = Path(os.environ.get("JARVIS_DAEMON_HOME") or REPO_ROOT)

# Dev-mode .env first (unchanged - a real repo-relative .env, found via
# load_dotenv()'s own cwd-based upward search), then the packaged app's
# own settings file at <JARVIS_DAEMON_HOME>/.env (docs/desktop-app-
# DESIGN.md §6/§8.6's .env-discovery gap, §8.8's first-run settings
# screen - see jarvis_server.internal.env_config's own docstring for the full
# rationale). Must happen before importing anything that reads
# os.environ - same rationale as mcp_server.py / the CLI ingest
# entrypoints.
load_env(_DAEMON_HOME)

from starlette.applications import Starlette  # noqa: E402
from starlette.middleware import Middleware  # noqa: E402
from starlette.middleware.base import BaseHTTPMiddleware  # noqa: E402
from starlette.middleware.trustedhost import TrustedHostMiddleware  # noqa: E402
from starlette.responses import JSONResponse, PlainTextResponse  # noqa: E402
from starlette.routing import Route  # noqa: E402

from jarvis_server.internal.migrate import run_migrations  # noqa: E402
from jarvis_server.internal import db  # noqa: E402
from jarvis_server.internal.graph_db import graph_db  # noqa: E402
from jarvis_server.internal.logging.logging_config import get_logger  # noqa: E402
from jarvis_server.daemon.protocol import API_VERSION  # noqa: E402
from jarvis_server.internal import telemetry  # noqa: E402
from jarvis_server.internal.errors import USER_MESSAGES, ErrorCode, classify  # noqa: E402
from jarvis_server.connectors.base import Connector  # noqa: E402
from jarvis_server.connectors.drive import DriveConnector  # noqa: E402
from jarvis_server.connectors.filesystem import FilesystemConnector  # noqa: E402
from jarvis_server.mcp_tools import document as document_mod  # noqa: E402
from jarvis_server.mcp_tools import hierarchy as hierarchy_mod  # noqa: E402
from jarvis_server.mcp_tools import search as search_mod  # noqa: E402
from jarvis_server.mcp_tools import sql_query as sql_query_mod  # noqa: E402
from jarvis_server.mcp_tools.models import (  # noqa: E402
    FileTypeStat,
    FileTypeStatsResult,
    ProcessingTotals,
    SearchFilters,
    SyncRootRecord,
    SyncRootsResult,
    ToolCallHistoryResult,
    ToolCallRecord,
)
from jarvis_server import pipeline as pipeline_mod  # noqa: E402
from jarvis_server.pipeline import run_ingest  # noqa: E402

logger = get_logger(__name__)

DISCOVERY_FILE = _DAEMON_HOME / ".jarvis_daemon.json"

# Only one ingest in flight at a time - see module docstring. A thread
# lock, not an asyncio one: the run holds it on its own thread
# (`_run_ingest_in_background`), outliving the request that started it.
_WRITE_LOCK = threading.Lock()

# Current ingest phase - see module docstring's "Ingest phase tracking"
# note. None until the first ingest of this process's lifetime starts;
# holds the last phase reached (typically PHASE_DONE) between ingests
# otherwise, which is harmless - /ingest/status's own `in_progress`
# field (derived from `_WRITE_LOCK.locked()`) is what tells a poller
# whether that phase is current or stale.
_ingest_status: dict = {"phase": None, "batch_progress": None, "progress": None, "outcome": None}

# Set by POST /ingest/cancel, checked by `_should_cancel` (passed to
# `run_ingest` as `on_cancel_requested`) at each between-phase/between-
# batch checkpoint - see pipeline.py's module docstring. A single flag,
# not a per-run token, for the same reason `_ingest_status` is a single
# dict: `_WRITE_LOCK` already guarantees at most one ingest in flight per
# daemon process.
_cancel_requested = False


def _set_ingest_phase(phase: str) -> None:
    _ingest_status["phase"] = phase
    _ingest_status["progress"] = None
    # batch_progress is only ever populated during the embed phase (via
    # _set_ingest_batch_progress, embed_and_index_chunks's on_batch). Once
    # the pipeline moves past it - organize, reconcile, done - it must not
    # keep reporting that phase's last counts (e.g. "100%"): a poller would
    # render the embed phase's finished progress bar under a completely
    # different phase label, reading as a hang instead of the new phase
    # actually running with no per-item counts of its own.
    if phase != pipeline_mod.PHASE_GENERATING_EMBEDDINGS:
        _ingest_status["batch_progress"] = None


def _set_ingest_batch_progress(
    current_batch: int, total_batches: int, processed_items: int, total_items: int,
) -> None:
    _ingest_status["batch_progress"] = {
        "current_batch": current_batch,
        "total_batches": total_batches,
        "processed_items": processed_items,
        "total_items": total_items,
    }


def _set_ingest_progress(processed: int, total: int | None, errors: int) -> None:
    _ingest_status["progress"] = {"processed": processed, "total": total, "errors": errors}


def _should_cancel() -> bool:
    return _cancel_requested

# Client-registration + shutdown state - see module docstring for the
# real bug this closes and why it has to work this way. Set by main()
# before server.run(); api_shutdown uses it to actually stop the process.
_registered_clients: set[str] = set()
_uvicorn_server = None


def _model_response(result) -> JSONResponse:
    """Every MCP tool wrapper returns a Pydantic model (success) or an
    `ErrorResponse` - both already dump to the exact JSON shape a real
    MCP client sees. Reusing that shape here (rather than inventing a
    second envelope) means the daemon's wire format IS the MCP wire
    format, not a translation of it."""
    return JSONResponse(result.model_dump(mode="json"))


def _build_fingerprint() -> list | None:
    """Must agree with jarvis.modules.daemon.client's copy - a client
    compares its own fingerprint against this to detect a daemon left
    running from a build a reinstall/update has since replaced on disk."""
    if not getattr(sys, "frozen", False):
        return None
    try:
        stat = os.stat(sys.executable)
        return [stat.st_size, stat.st_mtime_ns]
    except OSError:
        return None


async def health(request):
    return JSONResponse({
        "ok": True, "pid": os.getpid(), "build_fingerprint": _build_fingerprint(), "api_version": API_VERSION,
    })


async def api_register_client(request):
    client_id = str(uuid.uuid4())
    _registered_clients.add(client_id)
    logger.info("daemon: client %s registered (active clients: %d)", client_id, len(_registered_clients))
    return JSONResponse({"client_id": client_id})


async def api_shutdown(request):
    """A client's own request to retire, and to shut the daemon down if
    it was the last one relying on it - see module docstring for why
    this has to be an explicit, daemon-side decision rather than an
    OS-level process-tree kill."""
    body = await request.json()
    _registered_clients.discard(body["client_id"])
    remaining = len(_registered_clients)
    if remaining == 0 and not _WRITE_LOCK.locked() and _uvicorn_server is not None:
        logger.info("daemon: last registered client gone and no ingest in flight - shutting down")
        _uvicorn_server.should_exit = True
        return JSONResponse({"shutdown": True})
    logger.info(
        "daemon: shutdown requested but staying up (remaining_clients=%d, ingest_in_flight=%s)",
        remaining, _WRITE_LOCK.locked(),
    )
    return JSONResponse({"shutdown": False, "remaining_clients": remaining})


def _persist_tool_call(source: str, request_body, response_payload, success: bool, started_at: str, duration_ms: int) -> None:
    conn = db.get_connection()
    try:
        db.insert_tool_call(
            conn, "mcp_tool", source, request_body, response_payload, success, started_at,
            duration_ms=duration_ms,
        )
    finally:
        conn.close()


def _log_tool_call(source: str):
    """Wraps one of the 6 MCP-facing route handlers below to persist a
    `tool_calls` row (Stats & Activity page) for every call, success or
    failure, without changing the handler's own response. Reads the
    request body again here purely to log it - Starlette caches
    `Request.json()`/`.body()` on the request instance itself, so the
    wrapped handler's own `await request.json()` call afterward sees the
    same parsed value rather than re-reading (and failing on) an
    already-consumed stream."""

    def decorator(handler):
        @functools.wraps(handler)
        async def wrapper(request):
            started = datetime.now(timezone.utc)
            start_monotonic = time.monotonic()
            try:
                request_body = await request.json()
            except Exception:
                request_body = None

            response = await handler(request)

            try:
                response_payload = json.loads(response.body)
            except Exception:
                response_payload = None
                success = False
            else:
                success = response.status_code < 400 and not (
                    isinstance(response_payload, dict) and "error" in response_payload
                )
            duration_ms = round((time.monotonic() - start_monotonic) * 1000)
            telemetry.capture("tool_called", tool=source, success=success, duration_ms=duration_ms)

            await asyncio.to_thread(
                _persist_tool_call, source, request_body, response_payload, success,
                started.isoformat(), duration_ms,
            )
            return response

        return wrapper

    return decorator


# ---------------------------------------------------------------------
# Reads - each calls the existing public tool function directly. Every
# one of these already opens and closes its own connection internally
# (unchanged); asyncio.to_thread just keeps the (synchronous) DB work
# off the event loop so concurrent requests don't block each other.
# Each is also wrapped with `_log_tool_call` for the Stats & Activity
# page's tool-call history (see that decorator's docstring).
# ---------------------------------------------------------------------

@_log_tool_call("search")
async def api_search(request):
    body = await request.json()
    filters = body.get("filters")
    result = await asyncio.to_thread(
        search_mod.search,
        body["query"],
        SearchFilters(**filters) if filters else None,
        body.get("limit", search_mod.DEFAULT_LIMIT),
        body.get("cursor"),
    )
    return _model_response(result)


@_log_tool_call("get_hierarchy")
async def api_get_hierarchy(request):
    body = await request.json()
    result = await asyncio.to_thread(
        hierarchy_mod.get_hierarchy, body.get("root"), body.get("depth"),
    )
    return _model_response(result)


@_log_tool_call("browse")
async def api_browse(request):
    body = await request.json()
    result = await asyncio.to_thread(
        hierarchy_mod.browse, body["topic_id"], body.get("cursor"), body.get("limit"),
    )
    return _model_response(result)


@_log_tool_call("related")
async def api_related(request):
    body = await request.json()
    result = await asyncio.to_thread(
        hierarchy_mod.related, body["id"], body.get("limit"),
    )
    return _model_response(result)


@_log_tool_call("get_document")
async def api_get_document(request):
    body = await request.json()
    result = await asyncio.to_thread(
        document_mod.get_document, body.get("document_id"), body.get("title"),
    )
    return _model_response(result)


@_log_tool_call("query_db")
async def api_query_db(request):
    body = await request.json()
    result = await asyncio.to_thread(
        sql_query_mod.query_db,
        body["sql"],
        body.get("limit", sql_query_mod.DEFAULT_ROW_LIMIT),
    )
    return _model_response(result)


# ---------------------------------------------------------------------
# Stats & Activity - read-only, daemon-only routes (not MCP tools, no
# _log_tool_call) backing the desktop app's Stats & Activity page.
# ---------------------------------------------------------------------

def _get_file_type_stats() -> FileTypeStatsResult:
    conn = db.get_connection()
    try:
        totals = db.get_processing_totals(conn)
        by_extension = db.get_file_type_stats(conn)
    finally:
        conn.close()
    return FileTypeStatsResult(
        totals=ProcessingTotals(total_count=totals["total_count"], total_bytes=totals["total_bytes"]),
        by_extension=[FileTypeStat(**dict(row)) for row in by_extension],
    )


async def api_get_file_type_stats(request):
    result = await asyncio.to_thread(_get_file_type_stats)
    return _model_response(result)


def _get_tool_call_history(limit: int, before_id: Optional[int]) -> ToolCallHistoryResult:
    conn = db.get_connection()
    try:
        rows = db.get_recent_tool_calls(conn, limit, before_id)
    finally:
        conn.close()
    return ToolCallHistoryResult(calls=[ToolCallRecord(**dict(row)) for row in rows])


async def api_get_tool_call_history(request):
    limit = int(request.query_params.get("limit", 50))
    before_id_raw = request.query_params.get("before_id")
    before_id = int(before_id_raw) if before_id_raw is not None else None
    result = await asyncio.to_thread(_get_tool_call_history, limit, before_id)
    return _model_response(result)


def _get_sync_roots() -> SyncRootsResult:
    conn = db.get_connection()
    try:
        rows = db.get_sync_roots(conn)
    finally:
        conn.close()
    return SyncRootsResult(roots=[SyncRootRecord(**dict(row)) for row in rows])


async def api_get_sync_roots(request):
    result = await asyncio.to_thread(_get_sync_roots)
    return _model_response(result)


# ---------------------------------------------------------------------
# Writes - the daemon opens the connection itself (run_ingest takes an
# explicit conn, same as jarvis/cmd/filesystem_ingest.py and
# drive_ingest.py already do), serialized through _WRITE_LOCK.
# ---------------------------------------------------------------------

def _run_ingest_for(connector: Connector, organize_hierarchy: bool) -> dict:
    conn = db.get_connection()
    try:
        return run_ingest(
            conn, connector,
            organize_hierarchy=organize_hierarchy, live_graph_path=graph_db.DEFAULT_GRAPH_PATH,
            on_phase=_set_ingest_phase,
            on_batch_progress=_set_ingest_batch_progress,
            on_progress=_set_ingest_progress,
            on_cancel_requested=_should_cancel,
            trigger="gui",
        )
    finally:
        conn.close()


def _error_body(exc: Exception, phase: Optional[str]) -> dict:
    code = classify(exc, phase)
    telemetry.capture_error(exc, error_code=code.value, phase=phase)
    return {"error": {"code": code.value, "message": USER_MESSAGES[code]}}


async def _start_ingest(
    connector_name: str, make_connector: Callable[[], Connector], organize_hierarchy: bool, log_ctx: str,
) -> JSONResponse:
    """Shared by both ingest routes. Replies 202 as soon as the run has
    started; the run itself continues on its own thread and its outcome
    is served by GET /ingest/status. A sync can take hours (organize on a
    large corpus), longer than any HTTP request should be held open.

    `make_connector` is a thunk so `DriveConnector.__init__`'s eager
    validation (architecture.md) lands in this try/except and comes back
    as an immediate error instead of a started run. `log_ctx` is only for
    the exception log line."""
    global _cancel_requested
    # Rejected rather than queued: the daemon outlives the app, so a run
    # started (or cancelled) before a restart can still be finishing, and a
    # queued request would sit there showing that older run's status.
    if not _WRITE_LOCK.acquire(blocking=False):
        return JSONResponse(
            {"error": {
                "code": "ingest_in_progress",
                "message": "An earlier sync is still finishing. Try again once it's done.",
            }},
            status_code=409,
        )
    try:
        connector = make_connector()
    except Exception as exc:
        _WRITE_LOCK.release()
        logger.exception("daemon: ingest could not start %s", log_ctx)
        return JSONResponse(_error_body(exc, None), status_code=500)

    _ingest_status.update(
        phase=pipeline_mod.PHASE_READING_FILES, batch_progress=None, progress=None, outcome=None,
    )
    _cancel_requested = False
    threading.Thread(
        target=_run_ingest_in_background,
        args=(connector_name, connector, organize_hierarchy, log_ctx),
        name="jarvis-ingest", daemon=True,
    ).start()
    return JSONResponse({"started": True}, status_code=202)


def _run_ingest_in_background(
    connector_name: str, connector: Connector, organize_hierarchy: bool, log_ctx: str,
) -> None:
    """Holds `_WRITE_LOCK` (acquired by `_start_ingest`) for the whole run
    and always releases it. The outcome is what the ingest route used to
    return directly: the result, `{"cancelled": ...}`, or an error body."""
    telemetry.capture("sync_started", connector=connector_name)
    started = time.monotonic()
    outcome: Optional[dict] = None
    finished: dict = {}
    try:
        result = _run_ingest_for(connector, organize_hierarchy)
        outcome = result
        finished = {
            "status": "done",
            "files_total": result["sync"].get("pulled", 0),
            "files_failed": result["sync"].get("errors", 0),
        }
    except pipeline_mod.IngestCancelled as exc:
        logger.info("daemon: ingest cancelled %s", log_ctx)
        outcome = {"cancelled": True, "partial": exc.partial}
        finished = {"status": "cancelled"}
    except Exception as exc:
        logger.exception("daemon: ingest failed %s", log_ctx)
        phase = pipeline_mod.PHASE_IDS.get(_ingest_status["phase"])
        outcome = _error_body(exc, phase)
        finished = {"status": "failed", "error_code": outcome["error"]["code"], "phase": phase}
    finally:
        # Only a BaseException (e.g. interpreter shutdown) leaves no outcome;
        # a poller must still see the run end rather than wait forever.
        _ingest_status["outcome"] = outcome or {"error": {
            "code": ErrorCode.UNKNOWN.value, "message": USER_MESSAGES[ErrorCode.UNKNOWN],
        }}
        _WRITE_LOCK.release()
        telemetry.capture(
            "sync_finished", connector=connector_name,
            duration_s=round(time.monotonic() - started, 1), **finished,
        )


async def api_ingest_filesystem(request):
    body = await request.json()
    return await _start_ingest(
        FilesystemConnector.name, lambda: FilesystemConnector(body["path"]),
        body.get("organize_hierarchy", True), f"path={body.get('path')!r}",
    )


async def api_ingest_drive(request):
    body = await request.json()
    return await _start_ingest(
        DriveConnector.name, lambda: DriveConnector(body["folder_id"]),
        body.get("organize_hierarchy", True), f"folder_id={body.get('folder_id')!r}",
    )


_CONNECTORS_BY_NAME: dict[str, Callable[[str], Connector]] = {
    FilesystemConnector.name: FilesystemConnector,
    DriveConnector.name: DriveConnector,
}


def _remove_source(connector: Connector) -> int:
    conn = db.get_connection()
    try:
        return pipeline_mod.remove_source(conn, connector)
    finally:
        conn.close()


async def api_remove_sync_root(request):
    """Takes one source out of the library (`pipeline.remove_source`).
    Shares `_WRITE_LOCK` with ingest, and is refused rather than queued
    while a sync holds it - same reasoning as `_start_ingest`."""
    body = await request.json()
    make_connector = _CONNECTORS_BY_NAME.get(body.get("connector"))
    if make_connector is None:
        return JSONResponse(
            {"error": {"code": "unknown_connector", "message": "Jarvis doesn't know this kind of source."}},
            status_code=400,
        )
    if not _WRITE_LOCK.acquire(blocking=False):
        return JSONResponse(
            {"error": {
                "code": "ingest_in_progress",
                "message": "A sync is still running. Remove this source once it's done.",
            }},
            status_code=409,
        )
    try:
        connector = make_connector(body["root_id"])
        removed = await asyncio.to_thread(_remove_source, connector)
    except Exception as exc:
        logger.exception("daemon: remove source failed connector=%s", body.get("connector"))
        return JSONResponse(_error_body(exc, None), status_code=500)
    finally:
        _WRITE_LOCK.release()
    telemetry.capture("source_removed", connector=connector.name, files_total=removed)
    return JSONResponse({"removed": removed})


async def api_ingest_cancel(request):
    """Requests cancellation of whatever ingest is currently in flight -
    see `_cancel_requested`'s own docstring for why one flag is enough.
    Cooperative, not instant: the in-flight `run_ingest` call only checks
    this at its own between-phase/between-batch checkpoints (pipeline.py's
    module docstring), so the response here doesn't mean the ingest has
    actually stopped yet - the run's eventual `outcome` on GET
    /ingest/status (`{"cancelled": true, ...}`) is what confirms that."""
    global _cancel_requested
    if not _WRITE_LOCK.locked():
        return JSONResponse({"cancel_requested": False, "reason": "no ingest in flight"})
    _cancel_requested = True
    return JSONResponse({"cancel_requested": True})


async def api_ingest_status(request):
    """Polled by a client until its ingest run ends -
    see the module docstring's "Ingest phase tracking" note. `phase` is
    None (never set at all, `_ingest_status`'s initial state) exactly
    when `in_progress` is also False and no ingest has ever run in this
    process; once one has, `phase` holds whatever it last reached
    (typically PHASE_DONE) between ingests - `in_progress`, not `phase`
    on its own, is what tells a caller whether that value is current.

    `.get(...)`, not `[...]`: this route must always return a
    well-formed body - a client polls it in a tight loop for the
    duration of a real ingest, and a raised exception here would hit
    Starlette's default error response, not this route's own JSON
    shape. `.get()` costs nothing and survives a future mistake that
    resets `_ingest_status` to `{}` instead of mutating its keys in
    place (every current writer already does the latter -
    `_set_ingest_phase`/`_set_ingest_batch_progress` and the two
    `async with _WRITE_LOCK:` blocks above - but nothing enforces that a
    future one will).

    `batch_progress` is None until the embed phase's first batch
    completes (or for the whole run, on a pure cache-hit re-sync that
    embeds nothing) - a client should treat that the same as no
    progress bar to show yet, not as an error.

    `progress` is `{processed, total, errors}` for the current phase, or
    None before that phase's first item lands (and for all of reconcile).
    `total` can itself be None (Drive's incremental sync); `errors` counts
    files sync couldn't read so far and is 0 for every other phase.

    `cancel_requested` reflects a POST /ingest/cancel that landed but
    hasn't taken effect yet (cancellation is cooperative - see that
    route's docstring) - a client can use it to show "Cancelling..."
    instead of the ordinary phase text for the remainder of the run.

    `outcome` is the last finished run's result - exactly what an ingest
    POST used to return directly (the result dict, `{"cancelled": ...}`,
    or an `{"error": ...}` body) - and None while a run is in progress or
    before any has finished in this process."""
    in_progress = _WRITE_LOCK.locked()
    return JSONResponse({
        "phase": _ingest_status.get("phase"),
        "batch_progress": _ingest_status.get("batch_progress"),
        "progress": _ingest_status.get("progress"),
        "in_progress": in_progress,
        "cancel_requested": _cancel_requested,
        "outcome": None if in_progress else _ingest_status.get("outcome"),
    })


class _RejectBrowserOriginMiddleware(BaseHTTPMiddleware):
    """Binding to 127.0.0.1 only keeps other *machines* out - it does not
    mean only this app's own client can reach it. Any webpage the user
    has open can still fire a same-origin-policy-exempt POST at a
    loopback port (no CORS preflight on a "simple request" - a bare
    fetch() with no custom headers qualifies), which would let a
    malicious page trigger /ingest/* or /shutdown blind. Worse, DNS
    rebinding can make such a page share-origin with the daemon, letting
    it read back /query_db or /get_document responses too - full corpus
    exfiltration. `daemon.client`'s own `requests` calls never set an
    Origin header (only browsers do), so rejecting any request that has
    one costs the real client nothing."""

    async def dispatch(self, request, call_next):
        if "origin" in request.headers:
            return PlainTextResponse("Cross-origin requests are not allowed", status_code=403)
        return await call_next(request)


app = Starlette(
    routes=[
        Route("/health", health),
        Route("/clients/register", api_register_client, methods=["POST"]),
        Route("/shutdown", api_shutdown, methods=["POST"]),
        Route("/search", api_search, methods=["POST"]),
        Route("/get_hierarchy", api_get_hierarchy, methods=["POST"]),
        Route("/browse", api_browse, methods=["POST"]),
        Route("/related", api_related, methods=["POST"]),
        Route("/get_document", api_get_document, methods=["POST"]),
        Route("/query_db", api_query_db, methods=["POST"]),
        Route("/stats/file_types", api_get_file_type_stats, methods=["GET"]),
        Route("/stats/tool_calls", api_get_tool_call_history, methods=["GET"]),
        Route("/ingest/filesystem", api_ingest_filesystem, methods=["POST"]),
        Route("/ingest/drive", api_ingest_drive, methods=["POST"]),
        Route("/ingest/status", api_ingest_status, methods=["GET"]),
        Route("/ingest/cancel", api_ingest_cancel, methods=["POST"]),
        Route("/sync_roots", api_get_sync_roots, methods=["GET"]),
        Route("/sync_roots/remove", api_remove_sync_root, methods=["POST"]),
    ],
    # TrustedHostMiddleware blocks DNS-rebinding (a request whose Host
    # header isn't actually 127.0.0.1/localhost, even though it reached
    # this loopback-bound socket); the Origin check above blocks ordinary
    # cross-site requests from a browser tab. Neither alone is enough -
    # see each middleware's own docstring/comment.
    middleware=[
        Middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost"]),
        Middleware(_RejectBrowserOriginMiddleware),
    ],
)


def _preflight() -> None:
    """Create/migrate the database if needed, then confirm it's actually
    usable, before binding a port or accepting any request.

    Previously this only *checked* whether a database existed and told
    the user to run `python -m jarvis.cmd.migrate` themselves, refusing
    to start otherwise - a real problem for a packaged desktop app, whose
    user has no terminal to run that command in. `run_migrations()` is
    reused directly, unchanged (jarvis/cmd/migrate.py) - it's already
    idempotent (tracks applied migrations in `schema_migrations`, skips
    what's already run) and already creates the file if it doesn't
    exist (`sqlite3.connect()` does that on first open), so calling it
    unconditionally on every startup is correct whether the database is
    missing, brand new, or just has migrations pending - not just for
    the "doesn't exist yet" case. A real failure (a corrupted file, a
    migration that can't apply) is not swallowed: it's logged with the
    real exception and the daemon still refuses to start.

    Parent directories for both the db and the graph file are created
    here too, unconditionally - neither `sqlite3.connect()` nor
    LadybugDB create missing intermediate directories on their own
    (confirmed directly: LadybugDB raises a raw "Cannot open file"
    `RuntimeError` if the graph path's parent doesn't exist yet). This
    matters specifically because of *why* JARVIS_DAEMON_HOME derivation
    exists at all (see connection.py/graph_db.py) - an OS-standard
    per-user app-data directory is exactly the kind of path that does
    NOT exist until something creates it on first launch. Without this,
    auto-migration alone would still leave a fresh desktop-app install
    crashing on its very first ingest."""
    db.DEFAULT_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    graph_db.DEFAULT_GRAPH_PATH.parent.mkdir(parents=True, exist_ok=True)

    try:
        run_migrations(db.DEFAULT_DB_PATH)
    except Exception:
        logger.exception(
            "Failed to apply migrations to %s - refusing to start", db.DEFAULT_DB_PATH,
        )
        sys.exit(1)

    try:
        # `db.get_connection`'s own default parameter was bound to
        # DEFAULT_DB_PATH's value at import time - passing it explicitly
        # here (not `db.get_connection()`) is what actually respects a
        # test's `monkeypatch.setattr(daemon_server.db, "DEFAULT_DB_PATH", ...)`.
        conn = db.get_connection(db.DEFAULT_DB_PATH)
        try:
            embedded_chunks = db.count_chunk_vectors(conn)
        finally:
            conn.close()
    except Exception:
        # Migrations just succeeded above, so a failure here means
        # something beyond schema/pending-migration state - a genuinely
        # corrupted file, a permissions problem, or similar.
        logger.exception(
            "Database at %s migrated successfully but could not be opened/queried afterward",
            db.DEFAULT_DB_PATH,
        )
        sys.exit(1)

    if embedded_chunks == 0:
        logger.warning(
            "Database at %s has no embedded chunks yet - search will return an empty "
            "list until content is ingested.",
            db.DEFAULT_DB_PATH,
        )
    else:
        logger.info("Daemon preflight OK - %d embedded chunk(s) in %s", embedded_chunks, db.DEFAULT_DB_PATH)

    # A row still `in_progress` at this point predates this process - the
    # `_WRITE_LOCK` below guarantees at most one ingest per daemon
    # process, so a genuinely live run would require that same process,
    # which can't exist yet (this runs before the socket even binds).
    # See ingest_runs.mark_orphaned_ingest_runs_interrupted's docstring.
    conn = db.get_connection(db.DEFAULT_DB_PATH)
    try:
        swept = db.mark_orphaned_ingest_runs_interrupted(conn, datetime.now(timezone.utc).isoformat())
    finally:
        conn.close()
    if swept:
        logger.info("Daemon preflight: marked %d orphaned ingest run(s) as interrupted", swept)


def _write_discovery_file(port: int) -> None:
    DISCOVERY_FILE.write_text(json.dumps({"port": port, "pid": os.getpid()}))


def _prewarm_frozen_imports() -> None:
    """Force the first import of modules that are otherwise lazily
    imported from request-handling code, before any asyncio.to_thread
    worker exists to race on them.

    Frozen-only: PyInstaller's archive loader (pyimod01_archive.py) isn't
    safe against two threads each importing a still-uncached module for
    the first time. Confirmed directly in .jarvis_daemon.log - concurrent
    ingest of several files, and back-to-back /search calls, each hit
    zlib.error: incorrect header check at this exact loader frame, for
    unrelated modules (converters/html.py and converters/pdf.py's lazy
    `from markitdown import MarkItDown`; requests.utils.get_netrc_auth's
    lazy `import netrc`) - the common thread being first-use-under-
    concurrency, not any one module actually being corrupt on disk.
    Running from source never goes through this loader at all.

    A real tester hit the same crash again after this fix first shipped,
    for a module not yet on this list - so this covers the rest of the
    lazy, function-body imports actually reachable from a to_thread
    worker, found by tracing every asyncio.to_thread call site in this
    file through to its eventual imports (organize/clustering.py's
    UMAP/HDBSCAN/KMeans, deferred until a real ingest needs them per
    clustering.py's own module docstring; connectors/drive/auth.py's and
    sync.py's/fetch.py's lazily-imported google-auth/googleapiclient
    pieces, deferred until a real Drive sync runs per sync.py's own
    comment on why). All are hard requirements (requirements.txt), not
    optional extras like markitdown, so unguarded like netrc above - an
    ImportError here means the install itself is broken, not something
    to silently swallow.
    """
    if not getattr(sys, "frozen", False):
        return
    import netrc  # noqa: F401
    try:
        from markitdown import MarkItDown
        MarkItDown()
    except ImportError:
        pass
    import umap  # noqa: F401
    import hdbscan  # noqa: F401
    from sklearn.cluster import KMeans  # noqa: F401
    from google.auth.exceptions import RefreshError  # noqa: F401
    from google.auth.transport.requests import Request  # noqa: F401
    from google.oauth2.credentials import Credentials  # noqa: F401
    from google_auth_oauthlib.flow import InstalledAppFlow  # noqa: F401
    from googleapiclient.discovery import build  # noqa: F401
    from googleapiclient.errors import HttpError  # noqa: F401


def main() -> None:
    global _uvicorn_server
    import uvicorn

    _preflight()
    _prewarm_frozen_imports()
    telemetry.init("jarvis-daemon", _DAEMON_HOME)

    # Bind our own plain socket first so the OS-assigned port is known
    # before uvicorn starts (design doc §6.3: discovery via a well-known
    # file, not a fixed port), and hand it to uvicorn as an already-open
    # listener via `sockets=` - NOT `fd=`: uvicorn's `fd=` path assumes
    # `socket.AF_UNIX` (`socket.fromfd(config.fd, socket.AF_UNIX, ...)`
    # in uvicorn/server.py), which doesn't exist at all on Windows and
    # raises `AttributeError` immediately. `sockets=[...]` takes a plain
    # `socket.socket` and works identically on both platforms.
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    sock.listen(100)
    port = sock.getsockname()[1]
    _write_discovery_file(port)
    logger.info("Jarvis daemon starting on 127.0.0.1:%d (pid %d)", port, os.getpid())

    # log_config=None, not uvicorn's own default: that default builds a
    # DefaultFormatter, whose __init__ checks sys.stderr.isatty() to
    # decide on colored output - and under this app's frozen,
    # console=False build, sys.stderr is None (no real console attached
    # at all), so isatty() raises AttributeError, which dictConfig()
    # turns into "ValueError: Unable to configure formatter 'default'" -
    # confirmed as a real, reproduced crash: the daemon had already bound
    # its socket and written the discovery file by this point, so a
    # client could connect, but the process then died configuring
    # uvicorn's own logging before ever calling server.run(), and every
    # request (even /health) hung until the client's own timeout. This
    # app already has its own logging setup (internal.logging.
    # logging_config.get_logger, used everywhere including this
    # module's own `logger` above) - uvicorn's default console logging
    # was never being read by anyone in a windowed app with no console.
    server = uvicorn.Server(uvicorn.Config(app, log_level="info", log_config=None))
    _uvicorn_server = server
    try:
        server.run(sockets=[sock])
    finally:
        try:
            DISCOVERY_FILE.unlink(missing_ok=True)
        except OSError:
            pass
        telemetry.shutdown()


if __name__ == "__main__":
    main()
