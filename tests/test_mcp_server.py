"""`python -m jarvis.cmd.mcp_server` - the stdio proxy's tool functions and
startup handling (docs/local-server-architecture-DESIGN.md §10).

Before this file existed, mcp_server.py had zero test coverage and no
daemon-unreachable handling at all - a confirmed, separate bug (if the
daemon died mid-session, Claude Desktop would hang or fail unexplained).
These tests cover: each tool proxy delegates to the matching
`daemon.client` wrapper unchanged (both the healthy-daemon case - no
regression - and all three error tiers, which the wrapper already turns
into an `ErrorResponse` before it ever reaches here); `query_db` is
deliberately absent from this process's tool registration (held back
from beta testers/Claude Desktop - see mcp_server.py's own module
docstring); and `main()`'s own tier-1 (auto-start failure) handling
exits cleanly instead of starting the stdio server against a daemon
that was never reached, plus its client-registration/shutdown-on-exit
wiring (the real, confirmed orphaned-daemon fix - see
jarvis_server.daemon.server's module docstring for the full mechanism).

`@server.tool()` returns the original function unchanged (confirmed via
`inspect.getsource(MCPServer.tool)` - the decorator only registers as a
side effect), so `mcp_server.search` etc. are plain, directly callable
functions here, same as any other function under test.
"""

import subprocess
import sys
from pathlib import Path

import pytest

from jarvis.cmd import mcp_server
from jarvis.modules.daemon.client import (
    DaemonConnectionError,
    DaemonError,
    DaemonStartupError,
    DaemonTimeoutError,
)
from jarvis_server.mcp_tools.models import (
    BrowseResult,
    ErrorDetail,
    ErrorResponse,
    HierarchyResult,
    RelatedResult,
    SearchResult,
    TopicNode,
)

_ERROR = ErrorResponse(error=ErrorDetail(code="daemon_unavailable", message="nope"))
REPO_ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------
# query_db must not be registered as a tool on this process's server -
# held back from beta testers/Claude Desktop, without touching query_db
# itself (still real and callable elsewhere - see test_sql_query.py and
# test_daemon.py's own coverage of it).
# ---------------------------------------------------------------------

def test_mcp_server_module_defines_no_query_db_function():
    """Order-independent: this only inspects mcp_server.py's own
    namespace, not the shared MCPServer singleton (see the subprocess
    test below for why the singleton itself isn't a reliable thing to
    check in-process)."""
    assert not hasattr(mcp_server, "query_db")


def test_query_db_is_not_registered_as_an_mcp_tool_in_a_fresh_process():
    """A same-process check of the shared server singleton isn't
    reliable here: jarvis_server.mcp_tools.sql_query.py's own query_db
    ALSO carries a @server.tool() decorator onto that exact same
    singleton, as an import side effect - if test_sql_query.py or
    test_daemon.py (both import sql_query.py directly) happened to run
    first in this pytest session, query_db would appear "registered"
    regardless of whether mcp_server.py still defines one. mcp_server.py's
    own module docstring already documents why it deliberately never
    imports sql_query.py, for exactly this reason. Only a fresh
    interpreter - importing nothing else first - gives a real,
    order-independent answer."""
    script = (
        "from jarvis.cmd import mcp_server\n"
        "names = sorted(t.name for t in mcp_server.server._tool_manager.list_tools())\n"
        "print(names)\n"
        "assert 'query_db' not in names, names\n"
        "assert {'search', 'get_hierarchy', 'browse', 'related'} <= set(names), names\n"
        "print('OK')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "OK" in result.stdout


# ---------------------------------------------------------------------
# Each tool proxy: delegates to daemon.client's matching wrapper,
# unchanged, for both a successful result and an ErrorResponse.
# ---------------------------------------------------------------------

def test_search_delegates_to_daemon_client_and_returns_its_result_unchanged(monkeypatch):
    success = SearchResult(query="hello", results=[], next_cursor=None)
    captured = {}

    def _fake_search(query, filters=None, limit=10, cursor=None):
        captured.update(query=query, filters=filters, limit=limit, cursor=cursor)
        return success

    monkeypatch.setattr(mcp_server.daemon_client, "search", _fake_search)

    result = mcp_server.search("hello", limit=5, cursor="c1")

    assert result is success
    assert captured == {"query": "hello", "filters": None, "limit": 5, "cursor": "c1"}


def test_search_returns_the_error_response_daemon_client_gives_it(monkeypatch):
    monkeypatch.setattr(mcp_server.daemon_client, "search", lambda *a, **k: _ERROR)

    result = mcp_server.search("hello")

    assert result is _ERROR


def test_get_hierarchy_delegates_to_daemon_client(monkeypatch):
    success = HierarchyResult(topics=[], low_confidence=False, low_confidence_reason=None)
    monkeypatch.setattr(mcp_server.daemon_client, "get_hierarchy", lambda root, depth: success)

    assert mcp_server.get_hierarchy(root="r1", depth=2) is success


def test_get_hierarchy_returns_the_error_response_daemon_client_gives_it(monkeypatch):
    monkeypatch.setattr(mcp_server.daemon_client, "get_hierarchy", lambda *a, **k: _ERROR)

    assert mcp_server.get_hierarchy() is _ERROR


def test_browse_delegates_to_daemon_client(monkeypatch):
    node = TopicNode(id="t1", name="Topic", path="/t1", level=0)
    success = BrowseResult(topic=node, chunks=[], next_cursor=None)
    captured = {}

    def _fake_browse(topic_id, cursor=None, limit=None):
        captured.update(topic_id=topic_id, cursor=cursor, limit=limit)
        return success

    monkeypatch.setattr(mcp_server.daemon_client, "browse", _fake_browse)

    result = mcp_server.browse("t1", cursor="c", limit=10)

    assert result is success
    assert captured == {"topic_id": "t1", "cursor": "c", "limit": 10}


def test_browse_returns_the_error_response_daemon_client_gives_it(monkeypatch):
    monkeypatch.setattr(mcp_server.daemon_client, "browse", lambda *a, **k: _ERROR)

    assert mcp_server.browse("t1") is _ERROR


def test_related_delegates_to_daemon_client(monkeypatch):
    success = RelatedResult(chunks=[], topics=None)
    monkeypatch.setattr(mcp_server.daemon_client, "related", lambda id, limit=None: success)

    assert mcp_server.related("c1", limit=3) is success


def test_related_returns_the_error_response_daemon_client_gives_it(monkeypatch):
    monkeypatch.setattr(mcp_server.daemon_client, "related", lambda *a, **k: _ERROR)

    assert mcp_server.related("c1") is _ERROR


# ---------------------------------------------------------------------
# Every tool proxy, for all three tiers - daemon.client already turns
# each into the same ErrorResponse shape (tested in
# test_daemon_error_handling.py); this just confirms mcp_server.py's
# proxies don't accidentally let one leak through as a raised exception
# instead, which is exactly the bug this file exists to close.
# ---------------------------------------------------------------------

@pytest.mark.parametrize("exc_cls", [DaemonStartupError, DaemonConnectionError, DaemonTimeoutError])
@pytest.mark.parametrize("tool_name,call_args", [
    ("search", ("hello",)),
    ("get_hierarchy", ()),
    ("browse", ("t1",)),
    ("related", ("c1",)),
])
def test_every_tool_proxy_surfaces_an_error_response_not_a_raised_exception(
    monkeypatch, exc_cls, tool_name, call_args
):
    error_response = ErrorResponse(error=ErrorDetail(code="daemon_unavailable", message=str(exc_cls)))
    monkeypatch.setattr(mcp_server.daemon_client, tool_name, lambda *a, **k: error_response)

    tool_fn = getattr(mcp_server, tool_name)
    result = tool_fn(*call_args)

    assert isinstance(result, ErrorResponse)


# ---------------------------------------------------------------------
# main(): tier-1 (auto-start failure) exits cleanly instead of starting
# the stdio server against a daemon that was never reached - the
# specific gap the daemon rewrite is meant to close.
# ---------------------------------------------------------------------

def test_main_exits_cleanly_when_the_daemon_never_starts(monkeypatch):
    def _boom(*a, **k):
        raise DaemonStartupError("The Jarvis background service didn't start in time. Try restarting the app.")

    monkeypatch.setattr(mcp_server.daemon_client, "ensure_daemon_running", _boom)

    ran_server = {"called": False}
    monkeypatch.setattr(mcp_server.server, "run", lambda *a, **k: ran_server.update(called=True))

    with pytest.raises(SystemExit) as exc_info:
        mcp_server.main()

    assert exc_info.value.code == 1
    assert ran_server["called"] is False  # never gets to server.run() at all


def test_main_starts_the_stdio_server_when_the_daemon_is_reachable(monkeypatch):
    monkeypatch.setattr(mcp_server.daemon_client, "ensure_daemon_running", lambda: 54321)
    monkeypatch.setattr(mcp_server.daemon_client, "register_client", lambda: "client-1")
    monkeypatch.setattr(mcp_server.daemon_client, "request_shutdown", lambda client_id: None)

    ran_server = {}

    def _fake_run(transport):
        ran_server["transport"] = transport

    monkeypatch.setattr(mcp_server.server, "run", _fake_run)

    mcp_server.main()  # must not raise / exit

    assert ran_server["transport"] == "stdio"


# ---------------------------------------------------------------------
# main(): client registration + shutdown-on-exit - the real, confirmed
# orphaned-daemon fix (see jarvis_server.daemon.server's module
# docstring). Mirrors what MainWindow.closeEvent does for the GUI.
# ---------------------------------------------------------------------

def test_main_requests_shutdown_with_the_registered_client_id_on_clean_exit(monkeypatch):
    monkeypatch.setattr(mcp_server.daemon_client, "ensure_daemon_running", lambda: 54321)
    monkeypatch.setattr(mcp_server.daemon_client, "register_client", lambda: "client-abc")
    monkeypatch.setattr(mcp_server.server, "run", lambda transport: None)

    shutdown_calls = []
    monkeypatch.setattr(mcp_server.daemon_client, "request_shutdown", shutdown_calls.append)

    mcp_server.main()

    assert shutdown_calls == ["client-abc"]


def test_main_does_not_request_shutdown_when_registration_never_succeeded(monkeypatch):
    monkeypatch.setattr(mcp_server.daemon_client, "ensure_daemon_running", lambda: 54321)

    def _boom(*a, **k):
        raise DaemonConnectionError("nope")

    monkeypatch.setattr(mcp_server.daemon_client, "register_client", _boom)
    monkeypatch.setattr(mcp_server.server, "run", lambda transport: None)

    shutdown_calls = []
    monkeypatch.setattr(mcp_server.daemon_client, "request_shutdown", shutdown_calls.append)

    mcp_server.main()  # a failed registration must not crash startup

    assert shutdown_calls == []


def test_main_swallows_a_failed_shutdown_request_on_exit(monkeypatch):
    monkeypatch.setattr(mcp_server.daemon_client, "ensure_daemon_running", lambda: 54321)
    monkeypatch.setattr(mcp_server.daemon_client, "register_client", lambda: "client-abc")
    monkeypatch.setattr(mcp_server.server, "run", lambda transport: None)

    def _boom(client_id):
        raise DaemonTimeoutError("nope")

    monkeypatch.setattr(mcp_server.daemon_client, "request_shutdown", _boom)

    mcp_server.main()  # must not raise despite the shutdown request failing


def test_main_requests_shutdown_even_after_a_keyboard_interrupt(monkeypatch):
    monkeypatch.setattr(mcp_server.daemon_client, "ensure_daemon_running", lambda: 54321)
    monkeypatch.setattr(mcp_server.daemon_client, "register_client", lambda: "client-abc")

    def _interrupt(transport):
        raise KeyboardInterrupt

    monkeypatch.setattr(mcp_server.server, "run", _interrupt)

    shutdown_calls = []
    monkeypatch.setattr(mcp_server.daemon_client, "request_shutdown", shutdown_calls.append)

    mcp_server.main()  # KeyboardInterrupt is handled, not re-raised

    assert shutdown_calls == ["client-abc"]
