"""jarvis.modules.cloud.client - the cloud-server HTTP client. Monkeypatches
requests.request directly (no HTTP-mocking library in this repo's
dependencies yet) to exercise call()'s retry/error-tier logic without a
real server."""

import requests
import pytest

from jarvis.modules.cloud import client


class _FakeResponse:
    def __init__(self, status_code: int, body):
        self.status_code = status_code
        self._body = body

    def json(self):
        return self._body


def test_call_returns_status_and_body_for_any_completed_response(monkeypatch):
    monkeypatch.setattr(
        requests, "request",
        lambda *a, **k: _FakeResponse(401, {"error": "nope"}),
    )
    status, body = client.call("POST", "/feedback")
    assert (status, body) == (401, {"error": "nope"})


def test_call_retries_once_on_connection_error_then_succeeds(monkeypatch):
    calls = {"n": 0}

    def _flaky(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise requests.exceptions.ConnectionError("reset")
        return _FakeResponse(200, {"ok": True})

    monkeypatch.setattr(requests, "request", _flaky)
    monkeypatch.setattr(client.time, "sleep", lambda *_: None)

    status, body = client.call("GET", "/health")
    assert (status, body) == (200, {"ok": True})
    assert calls["n"] == 2


def test_call_raises_cloud_connection_error_after_exhausting_retry(monkeypatch):
    monkeypatch.setattr(
        requests, "request",
        lambda *a, **k: (_ for _ in ()).throw(requests.exceptions.ConnectionError("reset")),
    )
    monkeypatch.setattr(client.time, "sleep", lambda *_: None)

    with pytest.raises(client.CloudConnectionError):
        client.call("GET", "/health")


def test_call_raises_cloud_timeout_error_without_retry_when_disabled(monkeypatch):
    calls = {"n": 0}

    def _timeout(*a, **k):
        calls["n"] += 1
        raise requests.exceptions.Timeout()

    monkeypatch.setattr(requests, "request", _timeout)

    with pytest.raises(client.CloudTimeoutError):
        client.call("POST", "/auth/request", retry_on_timeout=False)
    assert calls["n"] == 1  # no retry


def test_submit_feedback_sends_bearer_header_and_severity(monkeypatch):
    captured = {}

    def _fake_request(method, url, json=None, timeout=None, headers=None):
        captured["method"] = method
        captured["url"] = url
        captured["headers"] = headers
        captured["json"] = json
        return _FakeResponse(200, {"issue_url": "https://example.com/1"})

    monkeypatch.setattr(requests, "request", _fake_request)

    status, body = client.submit_feedback(
        "tok-123", "search is slow", "blocking", "2026-10-01T12:00:00+00:00", "INFO: something happened",
    )
    assert status == 200
    assert captured["headers"] == {"Authorization": "Bearer tok-123"}
    assert captured["url"].endswith("/feedback")
    assert captured["json"] == {
        "body": "search is slow", "severity": "blocking",
        "client_timestamp": "2026-10-01T12:00:00+00:00", "log_excerpt": "INFO: something happened",
    }
