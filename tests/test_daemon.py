"""Local server daemon (docs/local-server-architecture-DESIGN.md).

Covers: WAL mode + busy_timeout are actually set on a real connection (a
pragma check, not an assumption); the daemon's HTTP routes for search /
get_hierarchy / browse / related / query_db / ingest produce the same
result as calling the underlying function directly (the daemon is a
routing layer, not a reimplementation); and the daemon process itself -
starting, responding on /health, stopping, and a client auto-starting one
when none is reachable.

The in-process route tests use Starlette's TestClient (no real socket,
no subprocess) - fast, and enough to prove the routing/serialization is
correct. Only the process-lifecycle tests at the bottom spawn a real
`python -m jarvis.cmd.daemon` subprocess, pointed at an isolated tmp_path
via JARVIS_DB_PATH / JARVIS_DAEMON_HOME so they never touch the real
repo's jarvis.db or .jarvis_daemon.* files.
"""

import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from jarvis_server.internal import db
from jarvis_server.internal.embed.embed import Embedder
from jarvis_server.internal.llm.llm import LLMClient
from jarvis_server.internal.migrate import MIGRATIONS_DIR, run_migrations
from jarvis_server.mcp_tools import hierarchy as hierarchy_mod
from jarvis_server.mcp_tools import search as search_mod
from jarvis_server.mcp_tools import sql_query as sql_query_mod
from jarvis_server.mcp_tools.hierarchy import _get_hierarchy_impl
import jarvis.modules.daemon.client as daemon_client
import jarvis_server.daemon.server as daemon_server
from jarvis_server.daemon.protocol import API_VERSION
import jarvis_server.pipeline as pipeline_mod
from jarvis_server.connectors.filesystem import FilesystemConnector
from jarvis_server.pipeline import run_ingest

REPO_ROOT = Path(__file__).resolve().parent.parent

# `search_mod.db`, `hierarchy_mod.db`, `daemon_server.db` are all the same
# shared `jarvis_server.internal.db` module object, not per-module copies - so
# `route_db` below patches `get_connection` globally, once, no matter how
# many of those names it's set through. Capturing the *original* function
# here, before any test runs, is what lets test-only helpers (seeding,
# and the "direct call" half of the ingest-equivalence test) keep
# opening whichever db_path they're actually given, unaffected by
# whatever route_db has redirected the ambient db.get_connection to.
_REAL_GET_CONNECTION = db.get_connection


class StubEmbedder(Embedder):
    """Deterministic, offline - same shape as test_pipeline.py's."""

    def embed(self, texts: list[str]) -> list[list[float]]:
        out = []
        for text in texts:
            seed = sum(ord(c) for c in text) or 1
            out.append([((seed * (i + 1)) % 97) / 97.0 for i in range(1024)])
        return out


class StubLLM(LLMClient):
    def complete(self, system, user, *, temperature=0.2, max_tokens=400):
        return '{"title": "Topic", "description": "desc."}'


def _vec(first_value: float) -> list[float]:
    return [first_value] + [0.0] * 1023


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "daemon_test.db"
    run_migrations(path)
    return path


@pytest.fixture
def route_db(monkeypatch, db_path: Path):
    """Point every module the daemon's routes call straight at db_path,
    and swap real embedding/LLM calls for offline stubs - the same
    monkeypatch points test_pipeline.py / test_search.py already use,
    just applied so the *daemon's* route handlers pick them up too.

    Also isolates the graph file - previously not overridden at all here.
    Every existing ingest-route test below happens to pass
    organize_hierarchy=False, which skips reconcile (and therefore the
    graph swap) entirely, so this was a latent gap rather than one
    actually hit by anything before now - but the one new test below
    deliberately exercises reconcile for real, and would otherwise write
    to the real repo's jarvis_graph.lbdb. Fixed here, for every test
    using this fixture, not just the new one."""
    monkeypatch.setattr(search_mod.db, "get_connection", lambda: _REAL_GET_CONNECTION(db_path))
    monkeypatch.setattr(search_mod, "OpenRouterVoyageEmbedder", StubEmbedder)
    monkeypatch.setattr(hierarchy_mod.db, "get_connection", lambda: _REAL_GET_CONNECTION(db_path))
    monkeypatch.setattr(sql_query_mod.db, "DEFAULT_DB_PATH", db_path)
    monkeypatch.setattr(daemon_server.db, "get_connection", lambda: _REAL_GET_CONNECTION(db_path))
    monkeypatch.setattr(daemon_server.graph_db, "DEFAULT_GRAPH_PATH", db_path.parent / "test_graph.lbdb")
    monkeypatch.setattr(pipeline_mod, "OpenRouterVoyageEmbedder", StubEmbedder)
    monkeypatch.setattr(pipeline_mod, "OpenRouterLLM", StubLLM)
    return db_path


@pytest.fixture
def client(route_db) -> TestClient:
    # base_url matters now: TestClient's own default Host (testserver)
    # is exactly what daemon.server's TrustedHostMiddleware exists to
    # reject - see that middleware's own docstring.
    return TestClient(daemon_server.app, base_url="http://127.0.0.1")


# ---------------------------------------------------------------------
# WAL mode + busy_timeout - a real pragma check, not an assumption
# ---------------------------------------------------------------------

def test_get_connection_enables_wal_and_busy_timeout(db_path):
    conn = db.get_connection(db_path)
    try:
        journal_mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        busy_timeout = conn.execute("PRAGMA busy_timeout").fetchone()[0]
    finally:
        conn.close()
    assert journal_mode.lower() == "wal"
    assert busy_timeout > 0

    # WAL is stored in the file itself (SQLite header), not per-connection
    # state - a second, completely independent connection to the same
    # file should see it too, without anyone setting it again.
    conn2 = db.get_connection(db_path)
    try:
        assert conn2.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    finally:
        conn2.close()


# ---------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------

def test_health_endpoint(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["pid"] == os.getpid()
    assert body["build_fingerprint"] is None  # not a frozen build under pytest
    assert body["api_version"] == API_VERSION


# ---------------------------------------------------------------------
# Build-fingerprint staleness - a reinstall replaces the packaged binary
# on disk while a daemon it spawned earlier may still be running.
# ---------------------------------------------------------------------

def test_live_port_reuses_a_daemon_with_no_fingerprint_opinion(monkeypatch):
    monkeypatch.setattr(daemon_client, "_read_discovery", lambda: {"port": 1234, "pid": 999})
    monkeypatch.setattr(
        daemon_client, "_health_body",
        lambda port: {"ok": True, "pid": 999, "build_fingerprint": None, "api_version": API_VERSION},
    )
    assert daemon_client._live_port() == 1234


def test_live_port_reuses_a_daemon_from_the_same_build(monkeypatch):
    monkeypatch.setattr(daemon_client, "_read_discovery", lambda: {"port": 1234, "pid": 999})
    monkeypatch.setattr(
        daemon_client, "_health_body",
        lambda port: {"ok": True, "pid": 999, "build_fingerprint": [333, 444], "api_version": API_VERSION},
    )
    monkeypatch.setattr(daemon_client, "_build_fingerprint", lambda: [333, 444])
    monkeypatch.setattr(
        daemon_client.os, "kill",
        lambda pid, sig: pytest.fail("must not kill a daemon from a matching build"),
    )
    assert daemon_client._live_port() == 1234


def test_live_port_kills_a_daemon_from_a_different_build(monkeypatch):
    monkeypatch.setattr(daemon_client, "_read_discovery", lambda: {"port": 1234, "pid": 999})
    monkeypatch.setattr(
        daemon_client, "_health_body", lambda port: {"ok": True, "pid": 999, "build_fingerprint": [111, 222]},
    )
    monkeypatch.setattr(daemon_client, "_build_fingerprint", lambda: [333, 444])
    killed = []
    monkeypatch.setattr(daemon_client.os, "kill", lambda pid, sig: killed.append((pid, sig)))

    assert daemon_client._live_port() is None
    assert killed == [(999, signal.SIGTERM)]


@pytest.mark.parametrize("health", [
    {"ok": True, "pid": 999, "build_fingerprint": None},  # too old to report a version
    {"ok": True, "pid": 999, "build_fingerprint": None, "api_version": API_VERSION - 1},
])
def test_live_port_kills_a_daemon_with_an_outdated_api(monkeypatch, health):
    """A packaged app's daemon (no fingerprint for a dev client to compare,
    or kept alive by Claude Desktop's MCP connection across an update)
    must not be reused by a client that needs a newer contract - the real
    case: a new client's ingest start hung on an old daemon that still ran
    the whole sync inside the request."""
    monkeypatch.setattr(daemon_client, "_read_discovery", lambda: {"port": 1234, "pid": 999})
    monkeypatch.setattr(daemon_client, "_health_body", lambda port: health)
    killed = []
    monkeypatch.setattr(daemon_client.os, "kill", lambda pid, sig: killed.append(pid))

    assert daemon_client._live_port() is None
    assert killed == [999]


def test_uvicorn_config_construction_survives_a_console_less_frozen_app():
    """Real, confirmed bug: sys.stdout/sys.stderr are the literal object
    None under this app's frozen, console=False windowed build (no
    console exists at all for Python to attach even a devnull-equivalent
    stream to - a real subprocess with output redirected to DEVNULL does
    NOT reproduce this, since that still gets a working file object).
    uvicorn's default log_config builds a DefaultFormatter, whose
    __init__ calls sys.stdout.isatty() to decide on colored output -
    AttributeError on None, which dictConfig() turns into "ValueError:
    Unable to configure formatter 'default'". Confirmed via a real
    PyInstaller build: the daemon had already bound its socket and
    written the discovery file by the time this crashed inside
    uvicorn.Config's own __init__, so a client could connect but nothing
    ever answered, including this literal /health route, until the
    client's own timeout - log_config=None (server.py's actual fix)
    skips that code path entirely. Monkeypatching sys.stdout/stderr
    directly, not via monkeypatch.setattr, so they're restored in
    `finally` before any of pytest's own output machinery could need
    them - this constructs uvicorn.Config only, no logging call happens
    during that construction."""
    import uvicorn

    from jarvis_server.daemon.server import app

    real_stdout, real_stderr = sys.stdout, sys.stderr
    try:
        sys.stdout = None
        sys.stderr = None
        uvicorn.Config(app, log_level="info", log_config=None)  # must not raise
    finally:
        sys.stdout, sys.stderr = real_stdout, real_stderr


# ---------------------------------------------------------------------
# Cross-origin protection - a browser tab is not a trusted local caller
# just because it can reach 127.0.0.1 (see _RejectBrowserOriginMiddleware
# and the TrustedHostMiddleware config in daemon/server.py).
# ---------------------------------------------------------------------

def test_request_with_an_origin_header_is_rejected(client):
    """The real daemon_client never sends this header - only a browser
    does. Rejecting it blind is what stops a malicious webpage from
    firing a same-origin-policy-exempt POST at the daemon."""
    resp = client.get("/health", headers={"Origin": "https://evil.example"})
    assert resp.status_code == 403


def test_request_with_a_spoofed_host_header_is_rejected(route_db):
    """Simulates DNS rebinding: a request that physically reaches this
    127.0.0.1-bound socket but claims a different Host - exactly what
    would let a rebound page share-origin with the daemon and read its
    responses."""
    rebind_client = TestClient(daemon_server.app, base_url="http://evil.example")
    resp = rebind_client.get("/health")
    assert resp.status_code == 400


def test_request_with_the_real_clients_host_and_no_origin_still_works(client):
    """The real daemon_client (requests, no Origin header, Host is
    whatever 127.0.0.1:<port> the URL implies) must not be caught by
    either new protection - both tests above would be worthless if this
    one also failed."""
    resp = client.get("/health")
    assert resp.status_code == 200


# ---------------------------------------------------------------------
# Client registration + shutdown - the real, confirmed orphaned-daemon
# fix (see daemon_server's own module docstring for the full mechanism
# and why an OS-level process-tree kill could never be the fix here).
# ---------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _reset_registered_clients():
    """_registered_clients is a module-level global on the real
    daemon_server module TestClient runs against - reset it before and
    after every test in this file (not just the ones about it) so a
    registration from one test can never leak into another."""
    daemon_server._registered_clients.clear()
    yield
    daemon_server._registered_clients.clear()


@pytest.fixture(autouse=True)
def _reset_ingest_status():
    """Same leak, same fix, for `_ingest_status` (the /ingest/status
    phase tracker) - without this, whichever ingest test happens to run
    first in the file leaves its last phase (typically PHASE_DONE)
    sitting there for every later test that never itself ingests."""
    daemon_server._ingest_status["phase"] = None
    daemon_server._ingest_status["batch_progress"] = None
    daemon_server._ingest_status["progress"] = None
    daemon_server._ingest_status["outcome"] = None
    daemon_server._cancel_requested = False
    yield
    daemon_server._ingest_status["phase"] = None
    daemon_server._ingest_status["batch_progress"] = None
    daemon_server._ingest_status["progress"] = None
    daemon_server._ingest_status["outcome"] = None
    daemon_server._cancel_requested = False


class _FakeUvicornServer:
    """Stands in for the real uvicorn.Server main() sets on
    daemon_server._uvicorn_server - only should_exit matters here."""
    def __init__(self):
        self.should_exit = False


def test_register_client_returns_a_unique_id_each_time(client):
    id1 = client.post("/clients/register", json={}).json()["client_id"]
    id2 = client.post("/clients/register", json={}).json()["client_id"]

    assert id1 != id2


def test_shutdown_with_the_only_registered_client_actually_signals_should_exit(client, monkeypatch):
    fake_server = _FakeUvicornServer()
    monkeypatch.setattr(daemon_server, "_uvicorn_server", fake_server)
    client_id = client.post("/clients/register", json={}).json()["client_id"]

    resp = client.post("/shutdown", json={"client_id": client_id})

    assert resp.json() == {"shutdown": True}
    assert fake_server.should_exit is True


def test_shutdown_stays_up_while_another_registered_client_remains(client, monkeypatch):
    fake_server = _FakeUvicornServer()
    monkeypatch.setattr(daemon_server, "_uvicorn_server", fake_server)
    client_a = client.post("/clients/register", json={}).json()["client_id"]
    client_b = client.post("/clients/register", json={}).json()["client_id"]

    resp = client.post("/shutdown", json={"client_id": client_a})

    assert resp.json() == {"shutdown": False, "remaining_clients": 1}
    assert fake_server.should_exit is False

    # client_b retiring later, once it's genuinely the last one, DOES shut it down.
    resp2 = client.post("/shutdown", json={"client_id": client_b})
    assert resp2.json() == {"shutdown": True}
    assert fake_server.should_exit is True


def test_a_second_ingest_is_rejected_while_one_is_still_in_flight(tmp_path, client, monkeypatch):
    class _LockedWriteLock:
        def locked(self):
            return True

        def acquire(self, blocking=True):
            return False

    monkeypatch.setattr(daemon_server, "_WRITE_LOCK", _LockedWriteLock())

    resp = client.post("/ingest/filesystem", json={"path": str(tmp_path)})

    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "ingest_in_progress"


def test_shutdown_does_not_stop_the_daemon_while_an_ingest_is_in_flight(client, monkeypatch):
    """Protects a CLI ingest that never registered as a client at all -
    registering for a short-lived, one-shot process wasn't judged worth
    the added surface (see daemon_server's own module docstring); the
    realistic risk window (an actual ingest running, not just "a CLI
    process exists somewhere") is covered via the existing _WRITE_LOCK
    instead, regardless of registered-client count."""
    fake_server = _FakeUvicornServer()
    monkeypatch.setattr(daemon_server, "_uvicorn_server", fake_server)
    client_id = client.post("/clients/register", json={}).json()["client_id"]

    class _LockedWriteLock:
        def locked(self):
            return True

    monkeypatch.setattr(daemon_server, "_WRITE_LOCK", _LockedWriteLock())

    resp = client.post("/shutdown", json={"client_id": client_id})

    assert resp.json() == {"shutdown": False, "remaining_clients": 0}
    assert fake_server.should_exit is False


# ---------------------------------------------------------------------
# Real process lifecycle for the same fix - actual daemon subprocesses,
# not the in-process TestClient above. Mirrors
# test_daemon_process_starts_responds_and_stops's own pattern below.
# ---------------------------------------------------------------------

def test_daemon_process_shuts_down_when_its_only_client_requests_it(isolated_daemon_env):
    # DEVNULL, not PIPE: real, confirmed deadlock risk otherwise - a real
    # subprocess whose stdout/stderr are piped but never read fills the
    # OS pipe buffer once it logs enough, and blocks trying to write
    # more; if that write happens to be holding logging's internal lock
    # (e.g. mid-shutdown), the process can never finish shutting down,
    # and this test's own proc.wait() below just times out waiting for a
    # process that's alive but stuck. None of these subprocess tests
    # ever read proc.stdout, so there's nothing PIPE was buying here.
    proc = subprocess.Popen(
        [sys.executable, "-m", "jarvis.cmd.daemon"],
        cwd=str(REPO_ROOT), env=os.environ.copy(),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + daemon_client._STARTUP_TIMEOUT_S
        info = None
        while time.monotonic() < deadline:
            info = daemon_client._read_discovery()
            if info and daemon_client._health_check(info["port"]):
                break
            time.sleep(0.2)
        assert info is not None, "daemon never wrote a discovery file"
        assert daemon_client._health_check(info["port"]), "daemon never answered /health"

        client_id = daemon_client.register_client()
        result = daemon_client.request_shutdown(client_id)

        assert result["shutdown"] is True
        proc.wait(timeout=10)
        assert proc.returncode is not None
        assert not daemon_client._health_check(info["port"]), "daemon still answering /health after shutdown"
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)


def test_daemon_process_stays_alive_for_another_registered_client(isolated_daemon_env):
    """The real test the orphaned-daemon fix needs: a daemon serving
    another active client (simulated here the way the task allowed - "a
    second reference", standing in for the CLI or a --mcp session) must
    NOT die just because one client (standing in for the GUI) asks it to
    retire."""
    proc = subprocess.Popen(
        [sys.executable, "-m", "jarvis.cmd.daemon"],
        cwd=str(REPO_ROOT), env=os.environ.copy(),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + daemon_client._STARTUP_TIMEOUT_S
        info = None
        while time.monotonic() < deadline:
            info = daemon_client._read_discovery()
            if info and daemon_client._health_check(info["port"]):
                break
            time.sleep(0.2)
        assert info is not None, "daemon never wrote a discovery file"
        assert daemon_client._health_check(info["port"]), "daemon never answered /health"

        client_a = daemon_client.register_client()  # stands in for the GUI
        client_b = daemon_client.register_client()  # stands in for the CLI / --mcp

        result_a = daemon_client.request_shutdown(client_a)
        assert result_a == {"shutdown": False, "remaining_clients": 1}
        assert daemon_client._health_check(info["port"]), "daemon must stay alive - client_b is still registered"

        result_b = daemon_client.request_shutdown(client_b)
        assert result_b["shutdown"] is True
        proc.wait(timeout=10)
        assert not daemon_client._health_check(info["port"])
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)


# ---------------------------------------------------------------------
# search() through the daemon vs. calling it directly
# ---------------------------------------------------------------------

def _seed_search_corpus(db_path: Path):
    conn = _REAL_GET_CONNECTION(db_path)
    try:
        conn.execute(
            "INSERT INTO documents (content_hash, normalized_markdown, first_seen_at, last_ingested_at) "
            "VALUES ('doc1', '# T', 'now', 'now')"
        )
        db.insert_document_source(
            conn, source_id="/tmp/doc1", connector="filesystem", content_hash="doc1",
            source_last_seen="now", title="doc1.md",
        )
        conn.execute(
            "INSERT INTO chunks (chunk_id, content_hash, ordinal, chunk_hash, text) "
            "VALUES ('c1', 'doc1', 0, 'c1', 'apple banana smoothie')"
        )
        conn.execute("INSERT INTO chunks_fts (chunk_id, text) VALUES ('c1', 'apple banana smoothie')")
        from jarvis_server.internal.embed.embed import _vector_to_blob
        conn.execute(
            "INSERT INTO chunk_vectors (chunk_id, embedding) VALUES ('c1', ?)",
            (_vector_to_blob(_vec(1.0)),),
        )
        conn.commit()
    finally:
        conn.close()


def test_search_through_daemon_matches_direct_call(client, route_db, monkeypatch):
    _seed_search_corpus(route_db)

    direct = search_mod.search("apple")

    resp = client.post("/search", json={"query": "apple", "limit": 10, "cursor": None})
    assert resp.status_code == 200
    body = resp.json()

    assert "error" not in body
    assert len(body["results"]) == len(direct.results) == 1
    assert body["results"][0]["chunk_id"] == direct.results[0].chunk_id == "c1"
    assert body["results"][0]["score"] == pytest.approx(direct.results[0].score)


def test_search_through_daemon_reports_errors_the_same_way(client):
    # No filters/cursor issue here - an unknown topic filter is the
    # cleanest way to exercise the error path both ways without seeding
    # real data.
    resp = client.post(
        "/search",
        json={"query": "x", "limit": 10, "cursor": "not-a-real-cursor"},
    )
    body = resp.json()
    assert "error" in body
    assert body["error"]["code"] == "invalid_argument"


# ---------------------------------------------------------------------
# get_hierarchy / browse / related through the daemon
# ---------------------------------------------------------------------

def _seed_topic_and_chunk(db_path: Path):
    conn = _REAL_GET_CONNECTION(db_path)
    try:
        conn.execute(
            "INSERT INTO topics (id, name, level, ancestor_path) VALUES ('t1', 'Topic One', 0, '[]')"
        )
        conn.execute(
            "INSERT INTO documents (content_hash, normalized_markdown, first_seen_at, last_ingested_at) "
            "VALUES ('doc1', '# T', 'now', 'now')"
        )
        db.insert_document_source(
            conn, source_id="/tmp/doc1", connector="filesystem", content_hash="doc1",
            source_last_seen="now", title="doc1.md",
        )
        conn.execute(
            "INSERT INTO chunks (chunk_id, content_hash, ordinal, chunk_hash, text, topic_id) "
            "VALUES ('c1', 'doc1', 0, 'c1', 'hello', 't1')"
        )
        conn.commit()
    finally:
        conn.close()


def test_get_hierarchy_through_daemon_matches_direct_call(client, route_db):
    _seed_topic_and_chunk(route_db)

    conn = _REAL_GET_CONNECTION(route_db)
    try:
        direct = _get_hierarchy_impl(conn, None, None)
    finally:
        conn.close()

    resp = client.post("/get_hierarchy", json={"root": None, "depth": None})
    body = resp.json()
    assert "error" not in body
    assert [t["id"] for t in body["topics"]] == [t.id for t in direct.topics]


def test_browse_through_daemon_matches_direct_call(client, route_db):
    _seed_topic_and_chunk(route_db)

    resp = client.post("/browse", json={"topic_id": "t1", "cursor": None, "limit": None})
    body = resp.json()
    assert "error" not in body
    assert body["topic"]["id"] == "t1"
    assert len(body["chunks"]) == 1
    assert body["chunks"][0]["chunk_id"] == "c1"


def test_browse_through_daemon_reports_not_found_the_same_way(client, route_db):
    resp = client.post("/browse", json={"topic_id": "no-such-topic", "cursor": None, "limit": None})
    body = resp.json()
    assert body["error"]["code"] == "not_found"


def test_related_through_daemon_unknown_id_is_not_found(client, route_db):
    resp = client.post("/related", json={"id": "no-such-id", "limit": None})
    body = resp.json()
    assert body["error"]["code"] == "not_found"


# ---------------------------------------------------------------------
# query_db through the daemon
# ---------------------------------------------------------------------

def test_query_db_through_daemon_matches_direct_call(client, route_db):
    _seed_topic_and_chunk(route_db)

    direct = sql_query_mod.query_db("SELECT id, name FROM topics")
    resp = client.post("/query_db", json={"sql": "SELECT id, name FROM topics", "limit": 50})
    body = resp.json()

    assert "error" not in body
    assert body["columns"] == direct.columns
    assert body["rows"] == direct.rows


def test_query_db_through_daemon_rejects_writes_the_same_way(client, route_db):
    resp = client.post("/query_db", json={"sql": "DELETE FROM topics", "limit": 50})
    body = resp.json()
    assert body["error"]["code"] == "invalid_argument"


# ---------------------------------------------------------------------
# Tool-call logging (_log_tool_call) + the two Stats & Activity routes
# ---------------------------------------------------------------------

def test_search_through_daemon_logs_a_tool_call(client, route_db):
    _seed_search_corpus(route_db)

    resp = client.post("/search", json={"query": "apple", "limit": 10, "cursor": None})
    assert resp.status_code == 200

    conn = _REAL_GET_CONNECTION(route_db)
    try:
        rows = db.get_recent_tool_calls(conn, limit=10)
    finally:
        conn.close()

    assert len(rows) == 1
    row = rows[0]
    assert row["call_type"] == "mcp_tool"
    assert row["source"] == "search"
    assert bool(row["success"]) is True
    assert row["duration_ms"] is not None
    assert "apple" in row["request"]


def test_search_through_daemon_logs_a_failed_tool_call_on_error(client, route_db):
    resp = client.post(
        "/search", json={"query": "x", "limit": 10, "cursor": "not-a-real-cursor"},
    )
    assert "error" in resp.json()

    conn = _REAL_GET_CONNECTION(route_db)
    try:
        rows = db.get_recent_tool_calls(conn, limit=10)
    finally:
        conn.close()

    assert len(rows) == 1
    assert bool(rows[0]["success"]) is False


def test_stats_file_types_route_returns_totals_and_by_extension(client, route_db):
    resp = client.get("/stats/file_types")
    assert resp.status_code == 200
    assert resp.json() == {"totals": {"total_count": 0, "total_bytes": 0}, "by_extension": []}


def test_stats_tool_calls_route_reflects_logged_calls(client, route_db):
    _seed_search_corpus(route_db)
    client.post("/search", json={"query": "apple", "limit": 10, "cursor": None})

    resp = client.get("/stats/tool_calls?limit=10")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["calls"]) == 1
    assert body["calls"][0]["source"] == "search"


# ---------------------------------------------------------------------
# run_ingest() through the daemon vs. calling it directly
# ---------------------------------------------------------------------

def _make_corpus(folder: Path) -> None:
    folder.mkdir(exist_ok=True)
    (folder / "a.md").write_text("# A\n\nHello from A.\n")
    (folder / "b.md").write_text("# B\n\nHello from B.\n")


def _wait_for_outcome(client, timeout_s: float = 30) -> dict:
    """What daemon_client._run_ingest does: poll /ingest/status until the
    run has ended, then return its outcome."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        status = client.get("/ingest/status").json()
        if not status["in_progress"] and status["outcome"] is not None:
            return status["outcome"]
        time.sleep(0.02)
    raise AssertionError("the ingest never finished")


def _ingest(client, body: dict, route: str = "/ingest/filesystem") -> dict:
    resp = client.post(route, json=body)
    assert resp.status_code == 202, resp.json()
    return _wait_for_outcome(client)


def test_ingest_filesystem_through_daemon_matches_direct_run_ingest(
    tmp_path, route_db, client
):
    # A second, independent db + corpus for the direct-call comparison,
    # so the two runs can't affect each other's content-hash dedup.
    direct_db = tmp_path / "direct.db"
    run_migrations(direct_db)
    direct_corpus = tmp_path / "direct_corpus"
    _make_corpus(direct_corpus)

    direct_conn = _REAL_GET_CONNECTION(direct_db)
    try:
        direct = run_ingest(
            direct_conn, FilesystemConnector(str(direct_corpus)),
            embedder=StubEmbedder(), organize_hierarchy=False,
        )
    finally:
        direct_conn.close()

    daemon_corpus = tmp_path / "daemon_corpus"
    _make_corpus(daemon_corpus)
    via_daemon = _ingest(client, {"path": str(daemon_corpus), "organize_hierarchy": False})

    assert via_daemon["sync"]["pulled"] == direct["sync"]["pulled"] == 2
    assert via_daemon["sync"]["errors"] == direct["sync"]["errors"] == 0
    assert via_daemon["chunks_created"] == direct["chunks_created"]
    assert via_daemon["embed"]["newly_embedded"] == direct["embed"]["newly_embedded"]
    assert via_daemon["hierarchy"] == direct["hierarchy"] == {"skipped": "organize_hierarchy=False"}

    # And it's genuinely searchable afterward through the same route.
    search_resp = client.post("/search", json={"query": "hello", "limit": 10, "cursor": None})
    hits = {r["chunk_id"] for r in search_resp.json()["results"]}
    assert len(hits) == 2


def test_ingest_filesystem_through_daemon_tolerates_a_missing_folder_like_direct_call(
    route_db, client
):
    """A nonexistent path is not an ingest error - FilesystemConnector's
    walk already logs and treats it as "nothing found" (verified against
    a direct run_ingest() call), so the daemon route must report the
    same 200/zero-pulled outcome, not synthesize a failure."""
    body = _ingest(client, {"path": "C:/definitely/does/not/exist/anywhere", "organize_hierarchy": False})
    assert body["sync"]["pulled"] == 0
    assert body["sync"]["errors"] == 0


def test_ingest_filesystem_respects_a_custom_graph_path_override(
    tmp_path, route_db, client, monkeypatch
):
    """JARVIS_GRAPH_PATH (docs/desktop-app-DESIGN.md's confirmed gap - the
    graph path was previously hardcoded with no override at all, and the
    daemon never passed live_graph_path through to run_ingest() either)
    - this proves both halves actually work together: point
    graph_db.DEFAULT_GRAPH_PATH somewhere other than route_db's own
    isolation path, ingest, and confirm the graph file materializes
    exactly there, not at route_db's default or the old hardcoded one."""
    # A path directly under tmp_path (which pytest already created), not
    # a new subdirectory - parent-directory creation is _preflight()'s
    # job (tested separately below) and isn't exercised by these
    # TestClient-based route tests, which never call it.
    custom_graph_path = tmp_path / "custom.lbdb"
    monkeypatch.setattr(daemon_server.graph_db, "DEFAULT_GRAPH_PATH", custom_graph_path)

    corpus = tmp_path / "graph_path_corpus"
    _make_corpus(corpus)

    # organize_hierarchy=True (unlike every other ingest test in this
    # file) - deliberately, since only reconcile's atomic swap actually
    # writes the graph file; a run with it False never touches the graph
    # at all and wouldn't prove anything about this override.
    outcome = _ingest(client, {"path": str(corpus), "organize_hierarchy": True})

    assert "error" not in outcome
    assert custom_graph_path.exists(), "the daemon did not write the graph file to the overridden path"


def test_ingest_drive_through_daemon_reports_a_real_error(route_db, client):
    """DriveConnector.__init__ raises ValueError eagerly on an empty
    folder_id (no whole-Drive mode) - a real, reliable way to exercise
    the daemon's error path without touching the real Drive API."""
    resp = client.post(
        "/ingest/drive",
        json={"folder_id": "", "organize_hierarchy": False},
    )
    assert resp.status_code == 500
    assert "error" in resp.json()


# ---------------------------------------------------------------------
# GET /ingest/status - the live-phase polling route (docs/desktop-app-
# ux-redesign-DESIGN.md §4c). test_pipeline.py already covers the
# on_phase callback contract in isolation; these confirm the daemon
# actually wires it up and that the route reflects a real, in-flight
# phase transition, not just the final result.
# ---------------------------------------------------------------------

def test_ingest_status_is_idle_before_any_ingest_has_run(client):
    resp = client.get("/ingest/status")
    assert resp.status_code == 200
    assert resp.json() == {"phase": None, "batch_progress": None, "progress": None, "in_progress": False, "cancel_requested": False, "outcome": None}


def test_ingest_status_returns_valid_json_even_if_ingest_status_is_missing_the_phase_key(
    client, monkeypatch
):
    """Real bug report investigated (manual testing: GET /ingest/status
    "sometimes returns a genuinely empty response body"). Extensive
    reproduction attempts - hammering this route with real HTTP
    requests, under a real uvicorn server, against every state named in
    the report (idle, mid-ingest at each phase transition, immediately
    after completion, and the daemon's own startup race window) - never
    once produced anything but a complete, valid JSON body; the route
    has exactly one, unconditional return path. Hardened anyway: this
    proves the route survives the one concrete way it COULD have failed
    - a future refactor resetting `_ingest_status` to `{}` instead of
    mutating its `"phase"` key in place, which `["phase"]` would have
    raised a KeyError on (an unhandled exception mid-request is exactly
    the kind of thing that could produce a malformed/incomplete
    response, even though Starlette's own default error handling
    normally still returns a real body, not an empty one)."""
    monkeypatch.setattr(daemon_server, "_ingest_status", {})

    resp = client.get("/ingest/status")

    assert resp.status_code == 200
    assert resp.json() == {"phase": None, "batch_progress": None, "progress": None, "in_progress": False, "cancel_requested": False, "outcome": None}


def test_ingest_status_reflects_done_after_a_successful_ingest(tmp_path, route_db, client):
    corpus = tmp_path / "corpus"
    _make_corpus(corpus)

    _ingest(client, {"path": str(corpus), "organize_hierarchy": False})

    status = client.get("/ingest/status").json()
    assert status["phase"] == pipeline_mod.PHASE_DONE
    assert status["in_progress"] is False


def test_ingest_status_reports_the_generating_embeddings_phase_while_genuinely_stuck_there(
    tmp_path, route_db, client, monkeypatch
):
    """Starts an ingest and blocks the pipeline's embed stage on a
    threading.Event this test controls - so /ingest/status, polled via a
    second, independent TestClient, can be confirmed to report a real mid-flight phase, not just
    whatever the route would trivially return if phases only changed
    instantaneously."""
    reached_embedding = threading.Event()
    release_embedding = threading.Event()
    real_embed_and_index_chunks = pipeline_mod.embed_and_index_chunks

    def _blocking_embed_and_index_chunks(conn, embedder, on_batch=None, **kwargs):
        reached_embedding.set()
        assert release_embedding.wait(timeout=10), "test never released the blocked embed stage"
        return real_embed_and_index_chunks(conn, embedder, on_batch=on_batch, **kwargs)

    monkeypatch.setattr(pipeline_mod, "embed_and_index_chunks", _blocking_embed_and_index_chunks)

    corpus = tmp_path / "corpus"
    _make_corpus(corpus)

    poll_client = TestClient(daemon_server.app, base_url="http://127.0.0.1")
    try:
        assert client.post(
            "/ingest/filesystem", json={"path": str(corpus), "organize_hierarchy": False},
        ).status_code == 202
        assert reached_embedding.wait(timeout=10), "ingest never reached the embed stage"

        status = poll_client.get("/ingest/status").json()
        assert status["phase"] == pipeline_mod.PHASE_GENERATING_EMBEDDINGS
        assert status["in_progress"] is True
    finally:
        release_embedding.set()

    assert "error" not in _wait_for_outcome(poll_client)
    final_status = poll_client.get("/ingest/status").json()
    assert final_status["phase"] == pipeline_mod.PHASE_DONE
    assert final_status["in_progress"] is False


def test_ingest_status_batch_progress_is_live_during_the_run(
    tmp_path, route_db, client, monkeypatch
):
    """/ingest/status's batch_progress (in-memory, via on_batch) must
    reflect a real mid-flight batch count, not just the final result -
    same discipline as the existing phase-transition test above."""
    reached_embedding = threading.Event()
    release_embedding = threading.Event()
    real_embed_and_index_chunks = pipeline_mod.embed_and_index_chunks

    def _blocking_embed_and_index_chunks(conn, embedder, on_batch=None, **kwargs):
        def _wrapped(*args):
            if on_batch is not None:
                on_batch(*args)
            reached_embedding.set()
            assert release_embedding.wait(timeout=10), "test never released the blocked embed stage"

        return real_embed_and_index_chunks(conn, embedder, on_batch=_wrapped, **kwargs)

    monkeypatch.setattr(pipeline_mod, "embed_and_index_chunks", _blocking_embed_and_index_chunks)

    corpus = tmp_path / "corpus"
    _make_corpus(corpus)

    poll_client = TestClient(daemon_server.app, base_url="http://127.0.0.1")
    try:
        assert client.post(
            "/ingest/filesystem", json={"path": str(corpus), "organize_hierarchy": False},
        ).status_code == 202
        assert reached_embedding.wait(timeout=10), "ingest never reached the embed stage"

        status = poll_client.get("/ingest/status").json()
        batch_progress = status["batch_progress"]
        assert batch_progress is not None
        assert batch_progress["total_items"] > 0
        assert batch_progress["current_batch"] == 0  # the pre-loop call this test blocks on
        assert status["progress"] == {
            "processed": 0, "total": batch_progress["total_items"], "errors": 0,
        }
    finally:
        release_embedding.set()

    assert "error" not in _wait_for_outcome(poll_client)


def test_ingest_status_clears_stale_batch_progress_once_past_the_embed_phase(
    tmp_path, route_db, client, monkeypatch
):
    """Real bug: batch_progress is only ever populated during the embed
    phase (via on_batch) - nothing reset it once the pipeline moved past
    it, so a poller kept rendering the embed phase's finished progress
    bar (100%) underneath the "Organizing into topics" phase label,
    reading as a hang instead of the next phase genuinely running with
    no per-item counts of its own. Blocks inside organize - reached only
    after embed has genuinely completed and left a real, non-None
    batch_progress behind - and confirms it's been cleared."""
    reached_organize = threading.Event()
    release_organize = threading.Event()
    real_run_organize = pipeline_mod._run_organize

    def _blocking_run_organize(conn, embedder, llm_client, reason, run_id, on_progress=None):
        reached_organize.set()
        assert release_organize.wait(timeout=10), "test never released the blocked organize stage"
        return real_run_organize(conn, embedder, llm_client, reason, run_id, on_progress)

    monkeypatch.setattr(pipeline_mod, "_run_organize", _blocking_run_organize)

    corpus = tmp_path / "corpus"
    _make_corpus(corpus)

    poll_client = TestClient(daemon_server.app, base_url="http://127.0.0.1")
    try:
        assert client.post(
            "/ingest/filesystem", json={"path": str(corpus), "organize_hierarchy": True},
        ).status_code == 202
        assert reached_organize.wait(timeout=10), "ingest never reached the organize stage"

        status = poll_client.get("/ingest/status").json()
        assert status["phase"] == pipeline_mod.PHASE_ORGANIZING
        assert status["batch_progress"] is None, (
            "batch_progress must not still show the embed phase's finished counts "
            "once organize is the current phase"
        )
        assert status["progress"] is None, "embed's item counts must not carry into organize"
    finally:
        release_organize.set()

    assert "error" not in _wait_for_outcome(poll_client)


def test_ingest_run_phases_table_reflects_the_completed_run(tmp_path, route_db, client):
    """ingest_run_phases (migration 0016) is written by embed_and_index_
    chunks itself, one commit per batch - a genuinely live,
    other-process-queryable record (e.g. via query_db), not just a
    same-process signal (that's /ingest/status's in-memory
    batch_progress, covered above). This confirms the row lands
    correctly, with status='done', once a real ingest actually
    completes - and that the run itself is recorded as 'done' with
    trigger='gui'."""
    corpus = tmp_path / "corpus"
    _make_corpus(corpus)

    run_id = _ingest(client, {"path": str(corpus), "organize_hierarchy": False})["run_id"]

    check_conn = db.get_connection()
    run_row = db.get_ingest_run(check_conn, run_id)
    embed_row = next(
        row for row in db.get_ingest_run_phases(check_conn, run_id)
        if row["phase"] == pipeline_mod.PHASE_GENERATING_EMBEDDINGS
    )
    check_conn.close()

    assert run_row["status"] == db.RUN_STATUS_DONE
    assert run_row["trigger"] == "gui"
    assert embed_row["status"] == db.PHASE_STATUS_DONE
    assert embed_row["total_items"] > 0
    assert embed_row["processed_items"] == embed_row["total_items"]
    assert embed_row["current_batch"] == embed_row["total_batches"]


def test_ingest_cancel_with_no_ingest_in_flight_is_a_no_op(client):
    resp = client.post("/ingest/cancel")
    assert resp.status_code == 200
    assert resp.json() == {"cancel_requested": False, "reason": "no ingest in flight"}


def test_ingest_cancel_stops_the_ingest_cooperatively(tmp_path, route_db, client, monkeypatch):
    """Mirrors test_ingest_status_batch_progress_is_live_during_the_run's
    blocking-embedder trick: pause the pipeline at the embed stage, call
    /ingest/cancel from a second client, then confirm the run's
    eventual outcome reflects the cancellation - cancellation only
    takes effect at the next between-batch checkpoint embed_and_index_
    chunks reaches, not the instant /ingest/cancel is called."""
    # batch_size=1: _make_corpus's 2 files must land as 2+ batches, or
    # there'd be no between-batch checkpoint left for the cancel flag to
    # be observed at (embed.py never checks after the final batch).
    monkeypatch.setattr(StubEmbedder, "batch_size", 1, raising=False)

    reached_embedding = threading.Event()
    release_embedding = threading.Event()
    real_embed_and_index_chunks = pipeline_mod.embed_and_index_chunks

    def _blocking_embed_and_index_chunks(conn, embedder, on_batch=None, **kwargs):
        reached_embedding.set()
        assert release_embedding.wait(timeout=10), "test never released the blocked embed stage"
        return real_embed_and_index_chunks(conn, embedder, on_batch=on_batch, **kwargs)

    monkeypatch.setattr(pipeline_mod, "embed_and_index_chunks", _blocking_embed_and_index_chunks)

    corpus = tmp_path / "corpus"
    _make_corpus(corpus)

    cancel_client = TestClient(daemon_server.app, base_url="http://127.0.0.1")
    try:
        assert client.post(
            "/ingest/filesystem", json={"path": str(corpus), "organize_hierarchy": False},
        ).status_code == 202
        assert reached_embedding.wait(timeout=10), "ingest never reached the embed stage"

        cancel_resp = cancel_client.post("/ingest/cancel")
        assert cancel_resp.json() == {"cancel_requested": True}
    finally:
        release_embedding.set()

    assert _wait_for_outcome(cancel_client)["cancelled"] is True


# ---------------------------------------------------------------------
# Real process lifecycle: start, /health, stop, and client auto-start.
# Isolated from the real repo via JARVIS_DB_PATH / JARVIS_DAEMON_HOME.
# ---------------------------------------------------------------------

def _kill(pid: int) -> None:
    try:
        os.kill(pid, signal.SIGTERM)
    except (ProcessLookupError, OSError):
        pass


@pytest.fixture
def isolated_daemon_env(tmp_path, monkeypatch):
    """A migrated db + daemon-home directory the real subprocess tests
    below use instead of the real repo's jarvis.db / .jarvis_daemon.*."""
    db_path = tmp_path / "subprocess_test.db"
    run_migrations(db_path)

    monkeypatch.setenv("JARVIS_DB_PATH", str(db_path))
    monkeypatch.setenv("JARVIS_DAEMON_HOME", str(tmp_path))
    # This process's own already-imported daemon_client module read
    # JARVIS_DAEMON_HOME at import time, long before this env var
    # existed - point its globals at the same place directly so the
    # *test's* calls to ensure_daemon_running/_live_port agree with
    # where the *subprocess* (a fresh interpreter, reads the env var
    # itself) will actually write its discovery file.
    monkeypatch.setattr(daemon_client, "DISCOVERY_FILE", tmp_path / ".jarvis_daemon.json")
    monkeypatch.setattr(daemon_client, "LOG_FILE", tmp_path / ".jarvis_daemon.log")
    monkeypatch.setattr(daemon_client, "_LOCK_FILE", tmp_path / ".jarvis_daemon.lock")
    return tmp_path


def test_daemon_process_starts_responds_and_stops(isolated_daemon_env):
    """Popen's `python -m jarvis.cmd.daemon` directly, deliberately with
    PYTHONHASHSEED left unset in the environment (same as
    `os.environ.copy()` here would inherit) - the exact condition that
    makes the daemon's own entrypoint relaunch itself into a
    correctly-seeded child (jarvis/cmd/daemon.py's `_ensure_fixed_hash_seed`,
    docs/organize-checkpointing-DESIGN.md §1d).

    Because of that, `proc` (the process this test Popens) is not
    guaranteed to be the process that actually ends up serving - it may
    hand off to a child with a different pid. This test doesn't assert
    pid equality for that reason; it uses the discovery file's pid (the
    same mechanism `ensure_daemon_running`'s own callers already rely on
    to find/stop the real daemon) to confirm and then stop the process
    that's actually listening, exactly like `_kill` is already used for
    below in `test_ensure_daemon_running_autostarts_when_nothing_is_listening`.
    """
    proc = subprocess.Popen(
        [sys.executable, "-m", "jarvis.cmd.daemon"],
        cwd=str(REPO_ROOT),
        env=os.environ.copy(),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + daemon_client._STARTUP_TIMEOUT_S
        info = None
        while time.monotonic() < deadline:
            info = daemon_client._read_discovery()
            if info and daemon_client._health_check(info["port"]):
                break
            time.sleep(0.2)
        assert info is not None, "daemon never wrote a discovery file"
        assert daemon_client._health_check(info["port"]), "daemon never answered /health"

        _kill(info["pid"])
        # Killing the real daemon (which may differ from `proc` after a
        # relaunch) unblocks `proc` on its own - it was either the same
        # process, or a launcher blocked in subprocess.run() waiting on
        # exactly this child.
        proc.wait(timeout=10)
        assert proc.returncode is not None
        assert not daemon_client._health_check(info["port"]), "daemon still answering /health after being killed"
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)


def test_preflight_creates_missing_parent_directories(tmp_path, monkeypatch):
    """Real, direct test of the mkdir fix - neither sqlite3.connect() nor
    LadybugDB create missing intermediate directories on their own
    (confirmed: the first version of this fix, without this mkdir, hit a
    real LadybugDB RuntimeError - "Cannot open file" - the moment a test
    pointed the graph path at a not-yet-existing directory). Fast,
    in-process - calls _preflight() directly rather than spawning a
    subprocess, since only the directory-creation behavior is being
    checked here, not the full daemon startup sequence."""
    fresh_home = tmp_path / "not_created_yet" / "nested"
    assert not fresh_home.exists()

    # daemon_server.db IS jarvis_server.internal.db (the same module object) -
    # patching it here is what _preflight() actually reads.
    monkeypatch.setattr(daemon_server.db, "DEFAULT_DB_PATH", fresh_home / "jarvis.db")
    monkeypatch.setattr(daemon_server.graph_db, "DEFAULT_GRAPH_PATH", fresh_home / "jarvis_graph.lbdb")

    daemon_server._preflight()

    assert (fresh_home / "jarvis.db").exists()
    assert (fresh_home / "jarvis_graph.lbdb").parent.exists()


def test_daemon_auto_migrates_a_missing_database_on_startup(tmp_path, monkeypatch):
    """The real gap this closes: _preflight() used to check for the db
    file and refuse to start with a "run `python -m jarvis.cmd.migrate`
    yourself" message - no good for a packaged desktop app's user, who
    has no terminal. Deliberately does NOT call run_migrations() first
    (unlike isolated_daemon_env) - db_path points at a file that has
    never existed, proving the daemon itself creates and migrates it,
    not that the test already did the work for it."""
    db_path = tmp_path / "never_existed.db"
    assert not db_path.exists()

    monkeypatch.setenv("JARVIS_DB_PATH", str(db_path))
    monkeypatch.setenv("JARVIS_DAEMON_HOME", str(tmp_path))
    monkeypatch.setattr(daemon_client, "DISCOVERY_FILE", tmp_path / ".jarvis_daemon.json")
    monkeypatch.setattr(daemon_client, "LOG_FILE", tmp_path / ".jarvis_daemon.log")
    monkeypatch.setattr(daemon_client, "_LOCK_FILE", tmp_path / ".jarvis_daemon.lock")

    proc = subprocess.Popen(
        [sys.executable, "-m", "jarvis.cmd.daemon"],
        cwd=str(REPO_ROOT),
        env=os.environ.copy(),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + daemon_client._STARTUP_TIMEOUT_S
        info = None
        while time.monotonic() < deadline:
            info = daemon_client._read_discovery()
            if info and daemon_client._health_check(info["port"]):
                break
            time.sleep(0.2)
        assert info is not None, "daemon never became healthy - did auto-migration fail?"
        assert daemon_client._health_check(info["port"])

        assert db_path.exists(), "the daemon never created the missing database file"
        conn = _REAL_GET_CONNECTION(db_path)
        try:
            applied = conn.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0]
            migration_files = len(list(MIGRATIONS_DIR.glob("*.sql")))
            assert applied == migration_files > 0, (
                f"expected all {migration_files} migrations applied, found {applied} rows"
            )
            # A real, non-bookkeeping table from a specific migration -
            # confirms the schema is actually usable, not just that the
            # tracking table itself exists.
            conn.execute("SELECT COUNT(*) FROM topics").fetchone()
        finally:
            conn.close()

        _kill(info["pid"])
        proc.wait(timeout=10)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)


def test_ensure_daemon_running_autostarts_when_nothing_is_listening(isolated_daemon_env):
    assert daemon_client._read_discovery() is None  # nothing running yet

    started_pid = None
    try:
        port = daemon_client.ensure_daemon_running(startup_timeout=15)
        assert daemon_client._health_check(port)

        info = daemon_client._read_discovery()
        started_pid = info["pid"]

        # A second call with a daemon already up must reuse it, not spawn
        # a second one.
        port_again = daemon_client.ensure_daemon_running(startup_timeout=15)
        assert port_again == port
        assert daemon_client._read_discovery()["pid"] == started_pid
    finally:
        if started_pid is not None:
            _kill(started_pid)


def test_ensure_daemon_running_does_not_double_spawn_when_racing(isolated_daemon_env, monkeypatch):
    """Regression test: releasing the spawn lock right after Popen()
    returns (rather than once the new daemon is actually healthy) let a
    second concurrent caller, still seeing no live port, win the
    now-free lock and spawn its own daemon too - the cause of running
    into 14 simultaneous daemon processes in practice. Spawning and
    health are faked (no real subprocess) so the race can be forced
    deterministically: `fake_spawn` takes noticeably longer to become
    healthy than the polling interval, giving a second caller, started
    a moment later, a real window to see "no live port" while the first
    caller's spawn is in flight."""
    spawn_calls = []
    healthy_after = {"deadline": None}

    def fake_spawn():
        spawn_calls.append(True)
        healthy_after["deadline"] = time.monotonic() + 0.5

    def fake_live_port():
        deadline = healthy_after["deadline"]
        if deadline is not None and time.monotonic() >= deadline:
            return 54321
        return None

    monkeypatch.setattr(daemon_client, "_spawn_daemon", fake_spawn)
    monkeypatch.setattr(daemon_client, "_live_port", fake_live_port)
    monkeypatch.setattr(daemon_client, "_POLL_INTERVAL_S", 0.05)

    results = []

    def call_ensure():
        results.append(daemon_client.ensure_daemon_running(startup_timeout=5))

    threads = [threading.Thread(target=call_ensure) for _ in range(5)]
    for i, t in enumerate(threads):
        t.start()
        if i == 0:
            time.sleep(0.1)  # let the first caller win the lock and start spawning
    for t in threads:
        t.join(timeout=10)

    assert len(spawn_calls) == 1, f"expected exactly one spawn, got {len(spawn_calls)}"
    assert results == [54321] * 5


# ---------------------------------------------------------------------
# POST /sync_roots/remove
# ---------------------------------------------------------------------

def test_remove_sync_root_takes_the_source_out_of_search_and_the_sources_list(
    tmp_path, route_db, client, monkeypatch
):
    events = []
    monkeypatch.setattr(daemon_server.telemetry, "capture", lambda event, **props: events.append((event, props)))
    corpus = tmp_path / "corpus"
    _make_corpus(corpus)
    _ingest(client, {"path": str(corpus), "organize_hierarchy": False})
    root_id = client.get("/sync_roots").json()["roots"][0]["root_id"]

    resp = client.post("/sync_roots/remove", json={"connector": "filesystem", "root_id": root_id})

    assert resp.status_code == 200
    assert resp.json() == {"removed": 2}
    assert client.get("/sync_roots").json()["roots"] == []
    search_resp = client.post("/search", json={"query": "hello", "limit": 10, "cursor": None})
    assert search_resp.json()["results"] == []
    assert ("source_removed", {"connector": "filesystem", "files_total": 2}) in events
    assert (corpus / "a.md").exists()


def test_remove_sync_root_is_refused_while_a_sync_is_running(tmp_path, client, monkeypatch):
    class _LockedWriteLock:
        def acquire(self, blocking=True):
            return False

    monkeypatch.setattr(daemon_server, "_WRITE_LOCK", _LockedWriteLock())

    resp = client.post("/sync_roots/remove", json={"connector": "filesystem", "root_id": str(tmp_path)})

    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "ingest_in_progress"


def test_remove_sync_root_rejects_an_unknown_connector(client):
    resp = client.post("/sync_roots/remove", json={"connector": "notion", "root_id": "x"})

    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "unknown_connector"
    assert not daemon_server._WRITE_LOCK.locked()


def test_remove_sync_root_failure_comes_back_as_a_calm_error_and_frees_the_lock(client, monkeypatch):
    def _fail(conn, connector):
        raise RuntimeError("boom at C:/Users/someone/secret.md")

    monkeypatch.setattr(daemon_server.pipeline_mod, "remove_source", _fail)

    resp = client.post("/sync_roots/remove", json={"connector": "filesystem", "root_id": "C:/x"})

    assert resp.status_code == 500
    assert resp.json()["error"]["code"] == "unknown"
    assert "secret" not in resp.json()["error"]["message"]
    assert not daemon_server._WRITE_LOCK.locked()
