"""Daemon-unreachable error handling (docs/local-server-architecture-DESIGN.md
§10): the three-tier exception hierarchy in jarvis/modules/daemon/client.py,
the exactly-one-retry behavior on a real per-request HTTP call, and the
read-tool wrappers' catch-and-return-ErrorResponse contract vs. the ingest
wrappers letting the exception propagate.

Also covers the ingest-timeout fix (docs/desktop-app-DESIGN.md §7, open
question #3): the two ingest wrappers use a longer, separate timeout than
every read, and skip the retry specifically on a timeout (unlike a
connection error, which still retries for every call kind).

No real sockets or subprocesses here - `ensure_daemon_running` is stubbed to
return a fixed port so these tests exercise `call()`'s own retry/tier logic
in isolation, fast and deterministically.
"""

import requests
import pytest

import jarvis.modules.daemon.client as daemon_client
from jarvis.modules.daemon.client import (
    DaemonConnectionError,
    DaemonStartupError,
    DaemonTimeoutError,
    call,
)
from jarvis_server.mcp_tools.models import ErrorResponse

_PORT = 54321


class _FakeResponse:
    def __init__(self, body, status_code=200):
        self._body = body
        self.status_code = status_code

    def json(self):
        return self._body


@pytest.fixture(autouse=True)
def _fast_and_fixed_port(monkeypatch):
    """Every test in this file talks to a stubbed "always up" daemon on a
    fixed port, with the retry pause shrunk to keep the suite fast."""
    monkeypatch.setattr(daemon_client, "ensure_daemon_running", lambda *a, **k: _PORT)
    monkeypatch.setattr(daemon_client, "_RETRY_DELAY_S", 0.0)


def _counting(side_effects):
    """Returns a fake `requests.request` that pops one entry off
    `side_effects` per call - an exception instance (raised), a plain
    dict (wrapped in a 200 `_FakeResponse`), or a (status_code, body)
    tuple (wrapped in a `_FakeResponse` with that status). Each recorded
    call is a (method, url, timeout) tuple so tests can also assert on
    which timeout a wrapper actually passed through."""
    calls = []

    def _fake_request(method, url, json=None, timeout=None):
        calls.append((method, url, timeout))
        effect = side_effects[len(calls) - 1]
        if isinstance(effect, Exception):
            raise effect
        if isinstance(effect, tuple):
            status_code, body = effect
            return _FakeResponse(body, status_code=status_code)
        return _FakeResponse(effect)

    return _fake_request, calls


# ---------------------------------------------------------------------
# call(): exactly one retry, then the right tier
# ---------------------------------------------------------------------

def test_call_retries_exactly_once_then_raises_connection_error(monkeypatch):
    fake_request, calls = _counting([
        requests.exceptions.ConnectionError("refused"),
        requests.exceptions.ConnectionError("refused"),
    ])
    monkeypatch.setattr(daemon_client.requests, "request", fake_request)

    with pytest.raises(DaemonConnectionError):
        call("POST", "/search", {})

    assert len(calls) == 2  # one attempt + exactly one retry, not a loop


def test_call_retries_exactly_once_then_raises_timeout_error(monkeypatch):
    fake_request, calls = _counting([
        requests.exceptions.Timeout("slow"),
        requests.exceptions.Timeout("slow"),
    ])
    monkeypatch.setattr(daemon_client.requests, "request", fake_request)

    with pytest.raises(DaemonTimeoutError):
        call("POST", "/search", {})

    assert len(calls) == 2


def test_call_non_json_response_is_treated_as_a_connection_error(monkeypatch):
    """A response that isn't valid JSON is grouped under the connection
    tier (module docstring), not a fourth tier - same one-retry contract."""
    fake_request, calls = _counting([
        ValueError("not json"),
        ValueError("not json"),
    ])
    monkeypatch.setattr(daemon_client.requests, "request", fake_request)

    with pytest.raises(DaemonConnectionError):
        call("POST", "/search", {})

    assert len(calls) == 2


def test_call_succeeds_on_the_retry_after_one_transient_failure(monkeypatch):
    """The whole point of the retry: a daemon that drops one request but
    answers the next must return the successful result, not an error."""
    fake_request, calls = _counting([
        requests.exceptions.ConnectionError("blip"),
        {"ok": True},
    ])
    monkeypatch.setattr(daemon_client.requests, "request", fake_request)

    result = call("POST", "/search", {})

    assert result == {"ok": True}
    assert len(calls) == 2


def test_call_succeeds_first_try_with_no_retry_at_all(monkeypatch):
    fake_request, calls = _counting([{"ok": True}])
    monkeypatch.setattr(daemon_client.requests, "request", fake_request)

    result = call("POST", "/search", {})

    assert result == {"ok": True}
    assert len(calls) == 1


def test_call_timeout_then_connection_error_on_retry_still_raises_a_tier(monkeypatch):
    """The retry's own failure mode doesn't have to match the first
    attempt's - either way it's still exactly one retry before raising."""
    fake_request, calls = _counting([
        requests.exceptions.Timeout("slow"),
        requests.exceptions.ConnectionError("refused"),
    ])
    monkeypatch.setattr(daemon_client.requests, "request", fake_request)

    with pytest.raises(DaemonConnectionError):
        call("POST", "/search", {})

    assert len(calls) == 2


# ---------------------------------------------------------------------
# ensure_daemon_running(): tier 1, not covered by call()'s retry at all
# ---------------------------------------------------------------------

def test_ensure_daemon_running_raises_daemon_startup_error_when_never_reachable(monkeypatch):
    monkeypatch.undo()  # this test manages its own stubs, not the autouse fixture's
    monkeypatch.setattr(daemon_client, "_live_port", lambda: None)
    monkeypatch.setattr(daemon_client, "_acquire_spawn_lock", lambda: True)
    monkeypatch.setattr(daemon_client, "_release_spawn_lock", lambda: None)
    monkeypatch.setattr(daemon_client, "_spawn_daemon", lambda: None)
    monkeypatch.setattr(daemon_client, "_POLL_INTERVAL_S", 0.01)

    with pytest.raises(DaemonStartupError):
        daemon_client.ensure_daemon_running(startup_timeout=0.05)


# ---------------------------------------------------------------------
# Read wrappers: catch all three tiers, return ErrorResponse
# ---------------------------------------------------------------------

@pytest.mark.parametrize("exc_cls,expected_code", [
    (DaemonStartupError, "daemon_unavailable"),
    (DaemonConnectionError, "daemon_unavailable"),
    (DaemonTimeoutError, "daemon_timeout"),
])
def test_search_returns_error_response_for_each_tier(monkeypatch, exc_cls, expected_code):
    def _boom(*a, **k):
        raise exc_cls("daemon trouble")

    monkeypatch.setattr(daemon_client, "call", _boom)

    result = daemon_client.search("query")

    assert isinstance(result, ErrorResponse)
    assert result.error.code == expected_code
    assert "daemon trouble" in result.error.message


@pytest.mark.parametrize("wrapper,args", [
    (daemon_client.get_hierarchy, ()),
    (daemon_client.browse, ("t1",)),
    (daemon_client.related, ("c1",)),
    (daemon_client.query_db, ("SELECT 1",)),
])
def test_every_read_wrapper_returns_error_response_on_daemon_connection_error(monkeypatch, wrapper, args):
    monkeypatch.setattr(daemon_client, "call", lambda *a, **k: (_ for _ in ()).throw(
        DaemonConnectionError("nope")
    ))

    result = wrapper(*args)

    assert isinstance(result, ErrorResponse)
    assert result.error.code == "daemon_unavailable"


# ---------------------------------------------------------------------
# Ingest wrappers: let the exception propagate, don't swallow it
# ---------------------------------------------------------------------

@pytest.mark.parametrize("exc_cls", [DaemonStartupError, DaemonConnectionError, DaemonTimeoutError])
def test_run_ingest_filesystem_propagates_every_tier(monkeypatch, exc_cls):
    monkeypatch.setattr(daemon_client, "call", lambda *a, **k: (_ for _ in ()).throw(exc_cls("x")))

    with pytest.raises(exc_cls):
        daemon_client.run_ingest_filesystem("/some/path")


@pytest.mark.parametrize("exc_cls", [DaemonStartupError, DaemonConnectionError, DaemonTimeoutError])
def test_run_ingest_drive_propagates_every_tier(monkeypatch, exc_cls):
    monkeypatch.setattr(daemon_client, "call", lambda *a, **k: (_ for _ in ()).throw(exc_cls("x")))

    with pytest.raises(exc_cls):
        daemon_client.run_ingest_drive("folder123")


# ---------------------------------------------------------------------
# Ingest runs: the daemon starts the run and replies at once; the wrapper
# waits for the outcome on /ingest/status (docs/observability-DESIGN.md
# §11 - a single long-held request made a still-working run look dead).
# ---------------------------------------------------------------------

_STARTED = (202, {"started": True})
_RUNNING = {"in_progress": True, "outcome": None}
_RESULT = {"run_id": "r", "sync": {"pulled": 1, "errors": 0}, "chunks_created": 1}
_DONE = {"in_progress": False, "outcome": _RESULT}


@pytest.fixture
def _no_poll_delay(monkeypatch):
    monkeypatch.setattr(daemon_client, "_INGEST_POLL_INTERVAL_S", 0)
    monkeypatch.setattr(daemon_client, "_read_discovery", lambda: {"port": _PORT, "pid": 1})


@pytest.mark.parametrize("wrapper,arg,route", [
    (daemon_client.run_ingest_filesystem, "/some/path", "/ingest/filesystem"),
    (daemon_client.run_ingest_drive, "folder123", "/ingest/drive"),
])
def test_ingest_wrappers_wait_for_the_runs_outcome(monkeypatch, _no_poll_delay, wrapper, arg, route):
    fake_request, calls = _counting([_STARTED, _RUNNING, _RUNNING, _DONE])
    monkeypatch.setattr(daemon_client.requests, "request", fake_request)

    assert wrapper(arg) == _RESULT
    assert [(m, url.split(str(_PORT))[-1]) for m, url, _ in calls] == [
        ("POST", route), ("GET", "/ingest/status"), ("GET", "/ingest/status"), ("GET", "/ingest/status"),
    ]


def test_an_ingest_that_could_not_start_returns_its_error_without_polling(monkeypatch, _no_poll_delay):
    busy = (409, {"error": {"code": "ingest_in_progress", "message": "still finishing"}})
    fake_request, calls = _counting([busy])
    monkeypatch.setattr(daemon_client.requests, "request", fake_request)

    assert daemon_client.run_ingest_filesystem("/some/path")["error"]["code"] == "ingest_in_progress"
    assert len(calls) == 1


def test_the_daemon_dying_mid_run_surfaces_as_unreachable(monkeypatch, _no_poll_delay):
    fake_request, _ = _counting([_STARTED, _RUNNING, requests.exceptions.ConnectionError("gone")])
    monkeypatch.setattr(daemon_client.requests, "request", fake_request)

    with pytest.raises(DaemonConnectionError):
        daemon_client.run_ingest_filesystem("/some/path")


def test_a_slow_status_answer_mid_run_keeps_waiting(monkeypatch, _no_poll_delay):
    """Heavy organize work can make the daemon slow to answer - that's
    busy, not gone, and must not fail a sync that's still running."""
    fake_request, _ = _counting([
        _STARTED, requests.exceptions.Timeout("busy"), requests.exceptions.Timeout("busy"), _DONE,
    ])
    monkeypatch.setattr(daemon_client.requests, "request", fake_request)

    assert daemon_client.run_ingest_filesystem("/some/path") == _RESULT


def test_status_polls_never_go_through_auto_start(monkeypatch, _no_poll_delay):
    """call()'s auto-start would treat a slow /health as a dead daemon and
    spawn a second one mid-sync - polling must bypass it."""
    fake_request, _ = _counting([_STARTED, _RUNNING, _DONE])
    monkeypatch.setattr(daemon_client.requests, "request", fake_request)
    started = []
    monkeypatch.setattr(daemon_client, "ensure_daemon_running", lambda *a, **k: started.append(1) or _PORT)

    daemon_client.run_ingest_filesystem("/some/path")

    assert started == [1]  # only for the POST that starts the run


def test_a_daemon_silent_for_too_long_mid_run_is_given_up_on(monkeypatch, _no_poll_delay):
    monkeypatch.setattr(daemon_client, "_INGEST_UNRESPONSIVE_S", 0)
    fake_request, _ = _counting([_STARTED, requests.exceptions.Timeout("silent")])
    monkeypatch.setattr(daemon_client.requests, "request", fake_request)

    with pytest.raises(DaemonTimeoutError):
        daemon_client.run_ingest_filesystem("/some/path")


@pytest.mark.parametrize("wrapper,args", [
    (daemon_client.search, ("query",)),
    (daemon_client.get_hierarchy, ()),
    (daemon_client.browse, ("t1",)),
    (daemon_client.related, ("c1",)),
    (daemon_client.query_db, ("SELECT 1",)),
])
def test_read_wrappers_still_use_the_original_default_timeout(monkeypatch, wrapper, args):
    # The response body doesn't match any wrapper's real success model -
    # irrelevant here, since all this test checks is the timeout value
    # `call()` passed down to the HTTP layer, not response validation.
    fake_request, calls = _counting([{"ok": True}])
    monkeypatch.setattr(daemon_client.requests, "request", fake_request)

    try:
        wrapper(*args)
    except Exception:
        pass

    assert len(calls) == 1
    assert calls[0][2] == daemon_client._DEFAULT_TIMEOUT_S == 120.0


def test_run_ingest_filesystem_does_not_retry_on_timeout_even_though_a_retry_would_have_succeeded(monkeypatch):
    """The core fix: a timed-out ingest must not be silently retried while
    the original may still be running behind the daemon's write lock -
    proven here by queuing a would-be-successful second response that a
    retry-based implementation would have consumed, and asserting it
    never gets touched."""
    fake_request, calls = _counting([
        requests.exceptions.Timeout("slow - daemon still working"),
        {"sync": {}, "chunks_created": 999},  # never reached if the fix is correct
    ])
    monkeypatch.setattr(daemon_client.requests, "request", fake_request)

    with pytest.raises(DaemonTimeoutError):
        daemon_client.run_ingest_filesystem("/some/path")

    assert len(calls) == 1  # no retry attempted


def test_run_ingest_drive_does_not_retry_on_timeout(monkeypatch):
    fake_request, calls = _counting([requests.exceptions.Timeout("slow")])
    monkeypatch.setattr(daemon_client.requests, "request", fake_request)

    with pytest.raises(DaemonTimeoutError):
        daemon_client.run_ingest_drive("folder123")

    assert len(calls) == 1


def test_read_wrapper_still_retries_once_on_timeout(monkeypatch):
    """Reaffirms reads keep the original retry-on-timeout behavior - only
    the two ingest wrappers opt out of it."""
    fake_request, calls = _counting([
        requests.exceptions.Timeout("slow"),
        {"ok": True},
    ])
    monkeypatch.setattr(daemon_client.requests, "request", fake_request)

    result = call("POST", "/search", {})

    assert result == {"ok": True}
    assert len(calls) == 2


def test_run_ingest_filesystem_connection_error_still_retries_once(monkeypatch):
    """Unchanged behavior: `retry_on_timeout=False` only gates the timeout
    branch - a refused/reset connection (the daemon never even started
    the request) still gets its one retry regardless of call kind."""
    monkeypatch.setattr(daemon_client, "_INGEST_POLL_INTERVAL_S", 0)
    monkeypatch.setattr(daemon_client, "_read_discovery", lambda: {"port": _PORT, "pid": 1})
    fake_request, calls = _counting([
        requests.exceptions.ConnectionError("refused"),
        _STARTED,
        _DONE,
    ])
    monkeypatch.setattr(daemon_client.requests, "request", fake_request)

    result = daemon_client.run_ingest_filesystem("/some/path")

    assert result == _RESULT
    assert [m for m, _, _ in calls] == ["POST", "POST", "GET"]


def test_call_retry_on_timeout_false_raises_immediately(monkeypatch):
    """Unit-level check of `call()` itself, independent of the ingest
    wrappers that use it."""
    fake_request, calls = _counting([requests.exceptions.Timeout("slow")])
    monkeypatch.setattr(daemon_client.requests, "request", fake_request)

    with pytest.raises(DaemonTimeoutError):
        call("POST", "/ingest/filesystem", {}, retry_on_timeout=False)

    assert len(calls) == 1


# ---------------------------------------------------------------------
# Status-code check: a non-2xx response is never silently handed back
# as if it were valid data (_do_request/_is_error_envelope).
# ---------------------------------------------------------------------

def test_call_raises_on_a_500_without_the_expected_error_envelope(monkeypatch):
    """A bare 500 (e.g. Starlette's own default error page, or any future
    route that doesn't build our `{"error": {...}}` shape) must not be
    silently returned as if it were a normal successful body - previously
    `_do_request` would have handed a caller `{"detail": "..."}` back as
    plain, unremarkable data."""
    fake_request, calls = _counting([
        (500, {"detail": "Internal Server Error"}),
        (500, {"detail": "Internal Server Error"}),
    ])
    monkeypatch.setattr(daemon_client.requests, "request", fake_request)

    with pytest.raises(DaemonConnectionError):
        call("POST", "/search", {})

    assert len(calls) == 2  # still gets the normal one retry, same as any other connection-tier failure


def test_call_raises_on_a_400_without_the_expected_error_envelope(monkeypatch):
    fake_request, calls = _counting([
        (400, {"detail": "Bad Request"}),
        (400, {"detail": "Bad Request"}),
    ])
    monkeypatch.setattr(daemon_client.requests, "request", fake_request)

    with pytest.raises(DaemonConnectionError):
        call("POST", "/search", {})

    assert len(calls) == 2


def test_call_returns_a_well_formed_500_error_envelope_unchanged(monkeypatch):
    """Regression guard for the existing, documented, tested ingest
    contract (server.py's api_ingest_filesystem/api_ingest_drive): a 500
    that DOES carry our own `{"error": {"code", "message"}}` shape is a
    deliberate, understood daemon-side failure - it must keep flowing
    through as plain data, not be turned into an exception, since every
    ingest caller already checks for this key explicitly."""
    body = {"error": {"code": "internal_error", "message": "boom"}}
    fake_request, calls = _counting([(500, body)])
    monkeypatch.setattr(daemon_client.requests, "request", fake_request)

    result = call("POST", "/ingest/filesystem", {})

    assert result == body
    assert len(calls) == 1  # no retry - this isn't treated as an anomaly at all


def test_run_ingest_filesystem_surfaces_a_malformed_500_as_an_error_not_data(monkeypatch):
    """End-to-end: a malformed non-2xx from an ingest call now raises
    DaemonConnectionError instead of returning a dict that looks like a
    normal (if odd-shaped) success - the exact "silently slip through"
    failure mode this fix closes."""
    fake_request, calls = _counting([
        (500, {"detail": "Internal Server Error"}),
        (500, {"detail": "Internal Server Error"}),
    ])
    monkeypatch.setattr(daemon_client.requests, "request", fake_request)

    with pytest.raises(DaemonConnectionError):
        daemon_client.run_ingest_filesystem("/some/path")

    assert len(calls) == 2


def test_read_wrapper_returns_error_response_for_a_malformed_500(monkeypatch):
    """The read-wrapper contract (catch DaemonError, return ErrorResponse)
    already existed for daemon-unreachable failures - confirms it also
    now covers this new failure mode without any change to `search()`
    itself, since it's still just a `DaemonConnectionError` under the
    hood."""
    fake_request, calls = _counting([
        (500, {"detail": "Internal Server Error"}),
        (500, {"detail": "Internal Server Error"}),
    ])
    monkeypatch.setattr(daemon_client.requests, "request", fake_request)

    result = daemon_client.search("query")

    assert isinstance(result, ErrorResponse)
    assert result.error.code == "daemon_unavailable"
    assert len(calls) == 2
