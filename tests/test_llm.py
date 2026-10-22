"""Transient-connection-failure retry for OpenRouterLLM.complete() (a real
ConnectionResetError was observed mid-TLS-handshake against openrouter.ai
during a live ingest, in this exact call - see jarvis/internal/llm/llm.py).
Mirrors tests/test_embed.py's retry coverage for
OpenRouterVoyageEmbedder._embed_batch() exactly, since the fix mirrors that
one precisely.
"""

import requests
import pytest

from jarvis_server.internal import db
from jarvis_server.internal.llm import llm
from jarvis_server.internal.llm.llm import LLMClient, LoggingLLMClient, OpenRouterLLM
from jarvis_server.internal.migrate import run_migrations


class _FakeResponse:
    def __init__(self, status_code: int):
        self.status_code = status_code

    def raise_for_status(self):
        err = requests.HTTPError(f"{self.status_code} Client Error: Unauthorized for url: ...")
        err.response = self
        raise err

    def json(self):  # pragma: no cover - not reached on the 401 path
        return {}


def test_non_auth_http_error_is_re_raised_unchanged(monkeypatch):
    monkeypatch.setenv("EMBEDDING_API_KEY", "sk-ok")
    monkeypatch.setattr(requests, "post", lambda *a, **k: _FakeResponse(500))

    with pytest.raises(requests.HTTPError):
        OpenRouterLLM().complete("system prompt", "user prompt")


# ---------------------------------------------------------------------
# Transient-connection-failure retry
# ---------------------------------------------------------------------

class _FakeCompletionResponse:
    """A successful chat-completion response - `raise_for_status()` is a
    no-op, `json()` returns one choice with the given content."""

    def __init__(self, content: str = "a label"):
        self._content = content

    def raise_for_status(self):
        pass

    def json(self):
        return {"choices": [{"message": {"content": self._content}}]}


def _counting_post(side_effects):
    """Fake `requests.post` that pops one entry off `side_effects` per
    call - an exception instance (raised) or a response-like object
    (returned). Returns (fake_post, calls) where `calls` is a plain
    counter list."""
    calls = []

    def _fake_post(*args, **kwargs):
        calls.append(1)
        effect = side_effects[len(calls) - 1]
        if isinstance(effect, Exception):
            raise effect
        return effect

    return _fake_post, calls


def test_complete_retries_on_transient_connection_error_then_succeeds(monkeypatch):
    monkeypatch.setenv("EMBEDDING_API_KEY", "sk-ok")
    monkeypatch.setattr(llm, "_LLM_RETRY_DELAY_S", 0.0)
    fake_post, calls = _counting_post([
        requests.exceptions.ConnectionError("reset"),
        _FakeCompletionResponse("Topic Label"),
    ])
    monkeypatch.setattr(requests, "post", fake_post)

    result = OpenRouterLLM().complete("system prompt", "user prompt")

    assert result == "Topic Label"
    assert len(calls) == 2  # one failure + the retry that succeeded


def test_complete_retries_on_timeout_then_succeeds(monkeypatch):
    monkeypatch.setenv("EMBEDDING_API_KEY", "sk-ok")
    monkeypatch.setattr(llm, "_LLM_RETRY_DELAY_S", 0.0)
    fake_post, calls = _counting_post([
        requests.exceptions.Timeout("slow"),
        _FakeCompletionResponse("Topic Label"),
    ])
    monkeypatch.setattr(requests, "post", fake_post)

    result = OpenRouterLLM().complete("system prompt", "user prompt")

    assert result == "Topic Label"
    assert len(calls) == 2


def test_complete_gives_up_after_max_retries_on_persistent_connection_error(monkeypatch):
    monkeypatch.setenv("EMBEDDING_API_KEY", "sk-ok")
    monkeypatch.setattr(llm, "_LLM_RETRY_DELAY_S", 0.0)
    monkeypatch.setattr(llm, "_LLM_MAX_RETRIES", 2)
    fake_post, calls = _counting_post([
        requests.exceptions.ConnectionError("reset"),
        requests.exceptions.ConnectionError("reset"),
        requests.exceptions.ConnectionError("reset"),
    ])
    monkeypatch.setattr(requests, "post", fake_post)

    with pytest.raises(requests.exceptions.ConnectionError):
        OpenRouterLLM().complete("system prompt", "user prompt")

    assert len(calls) == 3  # 1 original attempt + 2 retries, not a loop


def test_complete_does_not_retry_auth_failures(monkeypatch):
    """A 401/403 is a real, deterministic rejection - retrying would just
    waste time hitting the same wrong key again."""
    monkeypatch.setenv("EMBEDDING_API_KEY", "sk-wrong")
    fake_post, calls = _counting_post([_FakeResponse(401)])
    monkeypatch.setattr(requests, "post", fake_post)

    with pytest.raises(requests.HTTPError):
        OpenRouterLLM().complete("system prompt", "user prompt")

    assert len(calls) == 1


def test_complete_does_not_retry_other_http_errors(monkeypatch):
    monkeypatch.setenv("EMBEDDING_API_KEY", "sk-ok")
    fake_post, calls = _counting_post([_FakeResponse(500)])
    monkeypatch.setattr(requests, "post", fake_post)

    with pytest.raises(requests.HTTPError):
        OpenRouterLLM().complete("system prompt", "user prompt")

    assert len(calls) == 1


# ---------------------------------------------------------------------
# LoggingLLMClient - wraps another LLMClient, persisting a `tool_calls`
# row per physical .complete() call (see pipeline.py::_run_organize,
# which is the only real construction site).
# ---------------------------------------------------------------------

class _StubInnerLLM(LLMClient):
    """Deterministic, offline - a plain stand-in for whatever concrete
    client LoggingLLMClient wraps (real or already-stubbed)."""

    def __init__(self, response: str = "a label", to_raise: Exception | None = None):
        self.model = "stub-model"
        self._response = response
        self._to_raise = to_raise

    def complete(self, system, user, *, temperature=0.2, max_tokens=400):
        if self._to_raise is not None:
            raise self._to_raise
        return self._response


@pytest.fixture
def logging_conn(tmp_path):
    db_path = tmp_path / "logging_llm_test.db"
    run_migrations(db_path)
    conn = db.get_connection(db_path)
    yield conn
    conn.close()


def test_logging_llm_client_logs_a_successful_call(logging_conn):
    wrapped = LoggingLLMClient(_StubInnerLLM(response="a label"), logging_conn, run_id="run-1")

    result = wrapped.complete("sys prompt", "user prompt", temperature=0.1, max_tokens=100)

    assert result == "a label"
    rows = db.get_recent_tool_calls(logging_conn, limit=10)
    assert len(rows) == 1
    row = rows[0]
    assert row["call_type"] == "llm"
    assert row["source"] == "cluster_label"
    assert bool(row["success"]) is True
    assert row["error_message"] is None
    assert row["duration_ms"] is not None
    assert "run-1" in row["request"]
    assert "stub-model" in row["request"]
    assert "a label" in row["response"]


def test_logging_llm_client_logs_a_failed_call_and_reraises(logging_conn):
    wrapped = LoggingLLMClient(
        _StubInnerLLM(to_raise=RuntimeError("boom")), logging_conn, run_id="run-2",
    )

    with pytest.raises(RuntimeError, match="boom"):
        wrapped.complete("sys prompt", "user prompt")

    rows = db.get_recent_tool_calls(logging_conn, limit=10)
    assert len(rows) == 1
    row = rows[0]
    assert row["call_type"] == "llm"
    assert bool(row["success"]) is False
    assert row["error_message"] == "boom"
