"""Local MCP debug console (J-13).

Spawns `python -m jarvis.cmd.mcp_server` as a real subprocess and drives it
with the official MCP client SDK over stdio - the same handshake, JSON-RPC
framing, and `MCPServer.call_tool()` error translation a real client
(Claude Desktop, `claude mcp add`, an IDE plugin) exercises. This is
deliberately NOT a shortcut that imports search()/get_hierarchy()/browse()/
related() in-process: the point is to debug the underlying MCP transport
and server process, not just the tool functions.

    python -m jarvis.cmd.debug_ui        # http://127.0.0.1:8765

The server subprocess's own stderr (its preflight checks, its
`logger.exception(...)` tracebacks) is redirected to
`<repo>/.jarvis_debug_ui_server.log` rather than this process's terminal,
so the UI's "Server log" panel can tail it - otherwise the two processes'
stderr would interleave with no way to show just the one that matters.

The page itself is `static/index.html`, served as-is (no server-side
templating) - see that file for the client side of the `/api/*` routes
below.

The `/api/organize/*` routes are a separate feature bolted onto the same
console: a sandboxed playground for the clustering step
(`jarvis_server.organize.clustering`), read-only against the live
database and offline (no LLM/embedding API call - see
`jarvis.modules.debug_ui.playground`), so parameters can be tweaked and
re-run freely to see how the corpus actually clusters without touching
the real hierarchy or spending API calls.
"""

import asyncio
import json
import sys
import time
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from starlette.applications import Starlette
from starlette.responses import HTMLResponse, JSONResponse
from starlette.routing import Route

load_dotenv()

from jarvis_server.internal import db  # noqa: E402  (must follow load_dotenv)
from jarvis_server.internal.logging.logging_config import get_logger  # noqa: E402
from jarvis_server.organize import config as organize_config  # noqa: E402
from jarvis.modules.daemon import client as daemon_client  # noqa: E402
from jarvis.modules.daemon.client import DaemonError  # noqa: E402

from . import playground  # noqa: E402

logger = get_logger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
SERVER_LOG_PATH = REPO_ROOT / ".jarvis_debug_ui_server.log"
INDEX_HTML_PATH = Path(__file__).resolve().parent / "static" / "index.html"

# Cheap guard against an accidental huge playground run from the UI (a
# text field, not validated client-side beyond a number input) - the
# clustering itself has no such cap, this is purely a debug-UI courtesy.
PLAYGROUND_MAX_LIMIT = 20000

# Argument skeletons for the four known tools, shown as a starting point in
# the UI - not schema validation, just a click-to-fill convenience so a user
# does not need to hand-type SearchFilters' shape from memory.
EXAMPLE_ARGS = {
    "search": {"query": "", "limit": 10},
    "get_hierarchy": {"depth": 2},
    "browse": {"topic_id": "", "limit": 10},
    "related": {"id": "", "limit": 10},
}


class MCPDebugClient:
    """Owns one long-lived stdio session to a real `jarvis.cmd.mcp_server`
    subprocess for the lifetime of the debug UI process."""

    def __init__(self) -> None:
        self._stack = AsyncExitStack()
        self._log_file = None
        self.session: ClientSession | None = None
        self.tools: list[dict] = []

    async def start(self) -> None:
        self._log_file = open(SERVER_LOG_PATH, "w", encoding="utf-8")
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "jarvis.cmd.mcp_server"],
            cwd=str(REPO_ROOT),
        )
        read, write = await self._stack.enter_async_context(
            stdio_client(params, errlog=self._log_file)
        )
        self.session = await self._stack.enter_async_context(ClientSession(read, write))
        await self.session.initialize()
        listed = await self.session.list_tools()
        self.tools = [
            t.model_dump(mode="json", by_alias=True, include={"name", "description", "input_schema"})
            for t in listed.tools
        ]

    async def stop(self) -> None:
        await self._stack.aclose()
        if self._log_file is not None:
            self._log_file.close()


client = MCPDebugClient()


async def index(request):
    return HTMLResponse(INDEX_HTML_PATH.read_text(encoding="utf-8"))


async def api_tools(request):
    return JSONResponse({"tools": client.tools, "examples": EXAMPLE_ARGS})


async def api_call(request):
    body = await request.json()
    tool_name = body.get("tool", "")
    try:
        arguments = json.loads(body.get("arguments_json") or "{}")
    except json.JSONDecodeError as exc:
        return JSONResponse({"ok": False, "error": f"arguments is not valid JSON: {exc}"}, status_code=400)

    started = time.monotonic()
    try:
        result = await client.session.call_tool(tool_name, arguments)
        elapsed_ms = round((time.monotonic() - started) * 1000, 1)
        return JSONResponse({
            "ok": True,
            "elapsed_ms": elapsed_ms,
            "result": result.model_dump(mode="json", by_alias=True),
        })
    except Exception as exc:
        elapsed_ms = round((time.monotonic() - started) * 1000, 1)
        return JSONResponse({
            "ok": False,
            "elapsed_ms": elapsed_ms,
            "error": f"{type(exc).__name__}: {exc}",
        }, status_code=500)


async def api_organize_params(request):
    """The current effective value of every clustering knob, plus which
    names are valid to override - the playground form is built from this,
    not hardcoded in the HTML, so it can't drift from config.py."""
    return JSONResponse({
        "tunables": list(organize_config.TUNABLE_NAMES),
        "values": organize_config.snapshot(),
    })


async def api_organize_playground(request):
    """Run one sandboxed, offline re-cluster of the real corpus with the
    given parameter overrides. Reads the live database; never writes to
    it and never calls an LLM or embedding API (see playground.py)."""
    body = await request.json()
    overrides = body.get("overrides") or {}
    limit = body.get("limit")
    if limit is not None:
        try:
            limit = max(1, min(int(limit), PLAYGROUND_MAX_LIMIT))
        except (TypeError, ValueError):
            return JSONResponse({"ok": False, "error": "limit must be an integer"}, status_code=400)

    def _run() -> dict:
        conn = db.get_connection()
        try:
            restrict = db.get_chunk_ids_sample(conn, limit) if limit is not None else None
            return playground.run(conn, overrides=overrides, restrict_to_chunk_ids=restrict)
        finally:
            conn.close()

    started = time.monotonic()
    try:
        result = await asyncio.to_thread(_run)
        elapsed_ms = round((time.monotonic() - started) * 1000, 1)
        return JSONResponse({"ok": True, "elapsed_ms": elapsed_ms, "trace": result})
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    except Exception as exc:
        logger.exception("debug_ui: playground run failed")
        elapsed_ms = round((time.monotonic() - started) * 1000, 1)
        return JSONResponse({
            "ok": False, "elapsed_ms": elapsed_ms, "error": f"{type(exc).__name__}: {exc}",
        }, status_code=500)


def _resolve_limit(raw) -> Optional[int]:
    if raw is None:
        return None
    return max(1, min(int(raw), PLAYGROUND_MAX_LIMIT))


async def api_organize_session_start(request):
    """Start a step-by-step playground session: loads the corpus's real
    embeddings into memory (read-only) and returns a session_id. Nothing
    is clustered yet - the caller drives UMAP -> HDBSCAN -> labelling one
    stage at a time via /api/organize/session/step."""
    body = await request.json()
    try:
        limit = _resolve_limit(body.get("limit"))
    except (TypeError, ValueError):
        return JSONResponse({"ok": False, "error": "limit must be an integer"}, status_code=400)

    def _start() -> dict:
        conn = db.get_connection()
        try:
            restrict = db.get_chunk_ids_sample(conn, limit) if limit is not None else None
            return playground.start_session(conn, restrict_to_chunk_ids=restrict)
        finally:
            conn.close()

    try:
        result = await asyncio.to_thread(_start)
        return JSONResponse({"ok": True, **result})
    except Exception as exc:
        logger.exception("debug_ui: playground session start failed")
        return JSONResponse({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, status_code=500)


_STEP_FUNCS = {
    "umap": playground.run_umap_step,
    "hdbscan": playground.run_hdbscan_step,
    "label": playground.run_labeling_step,
}


async def api_organize_session_step(request):
    """Run one stage (umap | hdbscan | label) for one scope (base |
    rollup_root | rollup:<node_key>) of an existing session. Each stage
    reads the previous stage's cached result for that scope - see
    playground.py's "Sessions" section for the full scope/stage model."""
    body = await request.json()
    session_id = body.get("session_id")
    scope = body.get("scope")
    stage = body.get("stage")
    overrides = body.get("overrides") or {}
    if not session_id or not scope or stage not in _STEP_FUNCS:
        return JSONResponse({
            "ok": False,
            "error": "session_id, scope, and stage ('umap'|'hdbscan'|'label') are required",
        }, status_code=400)

    started = time.monotonic()
    try:
        result = await asyncio.to_thread(_STEP_FUNCS[stage], session_id, scope, overrides=overrides)
        elapsed_ms = round((time.monotonic() - started) * 1000, 1)
        return JSONResponse({"ok": True, "elapsed_ms": elapsed_ms, "result": result})
    except ValueError as exc:
        elapsed_ms = round((time.monotonic() - started) * 1000, 1)
        return JSONResponse({"ok": False, "elapsed_ms": elapsed_ms, "error": str(exc)}, status_code=400)
    except Exception as exc:
        logger.exception("debug_ui: playground session step failed (stage=%s scope=%s)", stage, scope)
        elapsed_ms = round((time.monotonic() - started) * 1000, 1)
        return JSONResponse({
            "ok": False, "elapsed_ms": elapsed_ms, "error": f"{type(exc).__name__}: {exc}",
        }, status_code=500)


async def api_organize_session_end(request):
    body = await request.json()
    session_id = body.get("session_id")
    if session_id:
        playground.end_session(session_id)
    return JSONResponse({"ok": True})


async def api_sync_roots(request):
    """The `sync_roots` table (jarvis/internal/db/sync_roots.py) - read
    directly against the live database, same pattern as the Organize
    playground routes above, since there's no MCP tool wrapping this."""
    conn = db.get_connection()
    try:
        rows = db.get_sync_roots(conn)
    finally:
        conn.close()
    return JSONResponse({"roots": [dict(row) for row in rows]})


async def api_sync_roots_resync(request):
    """Re-trigger a sync for one already-tracked (connector, root_id).

    Goes through the daemon's existing `/ingest/*` routes
    (jarvis.modules.daemon.client) - the same single-writer path the CLI
    ingest entrypoints (filesystem_ingest.py, drive_ingest.py) already
    use - never a direct `db.get_connection()` + `run_ingest()` call from
    this process, unlike the read-only Organize playground above. A
    second writer against jarvis.db/the graph file from here would be
    exactly the hazard the daemon's single-writer lock exists to prevent
    (see jarvis_server.daemon.server's module docstring)."""
    body = await request.json()
    connector = body.get("connector")
    root_id = body.get("root_id")
    if not connector or not root_id:
        return JSONResponse(
            {"ok": False, "error": "connector and root_id are required"}, status_code=400,
        )
    if connector not in ("filesystem", "drive"):
        return JSONResponse(
            {"ok": False, "error": f"no resync support for connector={connector!r}"}, status_code=400,
        )

    started = time.monotonic()
    try:
        if connector == "filesystem":
            result = await asyncio.to_thread(daemon_client.run_ingest_filesystem, root_id)
        else:
            result = await asyncio.to_thread(daemon_client.run_ingest_drive, root_id)
    except DaemonError as exc:
        elapsed_ms = round((time.monotonic() - started) * 1000, 1)
        return JSONResponse({"ok": False, "elapsed_ms": elapsed_ms, "error": str(exc)}, status_code=502)

    elapsed_ms = round((time.monotonic() - started) * 1000, 1)
    if "error" in result:
        return JSONResponse(
            {"ok": False, "elapsed_ms": elapsed_ms, "error": result["error"]["message"]}, status_code=500,
        )
    return JSONResponse({"ok": True, "elapsed_ms": elapsed_ms, "result": result})


async def api_ingest_status(request):
    """Proxies the daemon's `GET /ingest/status` (jarvis_server.daemon.
    server) so the "Synced roots" panel can poll live phase progress
    while its own Resync POST is still in flight. A daemon that can't be
    reached reads the same as "nothing running" - this is a polling
    convenience, not the resync call's own error path."""
    try:
        status = await asyncio.to_thread(daemon_client.get_ingest_status)
    except DaemonError:
        return JSONResponse({"phase": None, "in_progress": False})
    return JSONResponse(status)


async def api_log(request):
    if not SERVER_LOG_PATH.exists():
        return JSONResponse({"text": ""})
    text = SERVER_LOG_PATH.read_text(encoding="utf-8", errors="replace")
    return JSONResponse({"text": text[-20000:]})


@asynccontextmanager
async def lifespan(app):
    await client.start()
    try:
        yield
    finally:
        await client.stop()


app = Starlette(
    routes=[
        Route("/", index),
        Route("/api/tools", api_tools),
        Route("/api/call", api_call, methods=["POST"]),
        Route("/api/organize/params", api_organize_params),
        Route("/api/organize/playground", api_organize_playground, methods=["POST"]),
        Route("/api/organize/session/start", api_organize_session_start, methods=["POST"]),
        Route("/api/organize/session/step", api_organize_session_step, methods=["POST"]),
        Route("/api/organize/session/end", api_organize_session_end, methods=["POST"]),
        Route("/api/sync_roots", api_sync_roots),
        Route("/api/sync_roots/resync", api_sync_roots_resync, methods=["POST"]),
        Route("/api/ingest_status", api_ingest_status),
        Route("/api/log", api_log),
    ],
    lifespan=lifespan,
)


def main() -> None:
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8765, log_level="info")


if __name__ == "__main__":
    main()
