"""POST /feedback: session validation (this is the one place a bearer
token from sign-in gets checked), severity validation, and the
GitHub-issue side effect."""

from urllib.parse import parse_qs, urlparse

import pytest

from jarvis_cloud_server import server as server_mod
from jarvis_cloud_server.internal.db import connection as db_connection


def _link_params(url: str) -> tuple[str, str]:
    query = parse_qs(urlparse(url).query)
    return query["req"][0], query["token"][0]


@pytest.fixture
def session_token(client, email_sender) -> str:
    client.post("/auth/request", json={"email": "user@example.com"})
    _, url = email_sender.sent[0]
    req_param, token = _link_params(url)
    client.get("/auth/verify", params={"req": req_param, "token": token})
    status = client.get("/auth/status", params={"request_id": req_param}).json()
    return status["session_token"]


def test_feedback_requires_a_session(client):
    resp = client.post("/feedback", json={"body": "search feels slow", "severity": "annoying"})
    assert resp.status_code == 401


def test_feedback_rejects_a_bogus_token(client):
    resp = client.post(
        "/feedback", json={"body": "search feels slow", "severity": "annoying"},
        headers={"Authorization": "Bearer not-a-real-session"},
    )
    assert resp.status_code == 401


def test_feedback_rejects_a_revoked_session(client, session_token):
    with db_connection.get_connection() as conn:
        conn.execute("UPDATE sessions SET revoked_at = now()")
        conn.commit()

    resp = client.post(
        "/feedback", json={"body": "search feels slow", "severity": "annoying"},
        headers={"Authorization": f"Bearer {session_token}"},
    )
    assert resp.status_code == 401


def test_feedback_rejects_an_expired_session(client, session_token):
    from datetime import datetime, timedelta, timezone

    with db_connection.get_connection() as conn:
        conn.execute(
            "UPDATE sessions SET expires_at = %s",
            (datetime.now(timezone.utc) - timedelta(days=1),),
        )
        conn.commit()

    resp = client.post(
        "/feedback", json={"body": "search feels slow", "severity": "annoying"},
        headers={"Authorization": f"Bearer {session_token}"},
    )
    assert resp.status_code == 401


def test_feedback_opens_a_github_issue_for_a_valid_session(client, session_token, monkeypatch):
    monkeypatch.setattr(
        server_mod.feedback_github, "create_github_issue",
        lambda title, body: "https://github.com/example/jarvis/issues/999",
    )

    resp = client.post(
        "/feedback",
        json={"body": "search feels slow on my Drive folder", "severity": "blocking"},
        headers={"Authorization": f"Bearer {session_token}"},
    )

    assert resp.status_code == 200
    assert resp.json()["issue_url"] == "https://github.com/example/jarvis/issues/999"

    with db_connection.get_connection() as conn:
        row = conn.execute("SELECT body, severity, github_issue_url FROM feedback").fetchone()
    assert row[0] == "search feels slow on my Drive folder"
    assert row[1] == "blocking"
    assert row[2] == "https://github.com/example/jarvis/issues/999"


def test_feedback_still_saves_the_report_if_github_call_fails(client, session_token, monkeypatch):
    def _boom(title, body):
        raise RuntimeError("GitHub API unreachable")

    monkeypatch.setattr(server_mod.feedback_github, "create_github_issue", _boom)

    resp = client.post(
        "/feedback", json={"body": "search feels slow", "severity": "minor"},
        headers={"Authorization": f"Bearer {session_token}"},
    )

    assert resp.status_code == 502
    with db_connection.get_connection() as conn:
        row = conn.execute("SELECT body, severity, github_issue_url FROM feedback").fetchone()
    assert row[0] == "search feels slow"
    assert row[1] == "minor"
    assert row[2] is None


def test_feedback_rejects_an_empty_body(client, session_token):
    resp = client.post(
        "/feedback", json={"body": "   ", "severity": "annoying"},
        headers={"Authorization": f"Bearer {session_token}"},
    )
    assert resp.status_code == 400


def test_feedback_rejects_a_missing_severity(client, session_token):
    resp = client.post(
        "/feedback", json={"body": "search feels slow"},
        headers={"Authorization": f"Bearer {session_token}"},
    )
    assert resp.status_code == 400


def test_feedback_rejects_an_invalid_severity(client, session_token):
    resp = client.post(
        "/feedback", json={"body": "search feels slow", "severity": "catastrophic"},
        headers={"Authorization": f"Bearer {session_token}"},
    )
    assert resp.status_code == 400


def test_feedback_severity_is_case_insensitive(client, session_token, monkeypatch):
    monkeypatch.setattr(
        server_mod.feedback_github, "create_github_issue",
        lambda title, body: "https://github.com/example/jarvis/issues/1000",
    )
    resp = client.post(
        "/feedback", json={"body": "search feels slow", "severity": "Blocking"},
        headers={"Authorization": f"Bearer {session_token}"},
    )
    assert resp.status_code == 200


def test_feedback_without_timestamp_or_log_excerpt_still_works(client, session_token, monkeypatch):
    """Regression test: an older client that hasn't picked up
    client_timestamp/log_excerpt yet must never 400 just for omitting
    them."""
    monkeypatch.setattr(
        server_mod.feedback_github, "create_github_issue",
        lambda title, body: "https://github.com/example/jarvis/issues/1001",
    )
    resp = client.post(
        "/feedback", json={"body": "search feels slow", "severity": "minor"},
        headers={"Authorization": f"Bearer {session_token}"},
    )
    assert resp.status_code == 200


def test_feedback_includes_timestamp_and_log_excerpt_in_the_github_issue(client, session_token, monkeypatch):
    captured = {}
    monkeypatch.setattr(
        server_mod.feedback_github, "create_github_issue",
        lambda title, body: captured.update(body=body) or "https://github.com/example/jarvis/issues/1002",
    )
    resp = client.post(
        "/feedback",
        json={
            "body": "search feels slow",
            "severity": "annoying",
            "client_timestamp": "2026-10-01T12:00:00+00:00",
            "log_excerpt": "2026-10-01 12:59:30,000 INFO daemon: health check ok",
        },
        headers={"Authorization": f"Bearer {session_token}"},
    )

    assert resp.status_code == 200
    assert "**Reported at:** 2026-10-01T12:00:00+00:00" in captured["body"]
    assert "<details>" in captured["body"]
    assert "health check ok" in captured["body"]

    with db_connection.get_connection() as conn:
        row = conn.execute("SELECT client_timestamp, log_excerpt FROM feedback").fetchone()
    assert row[0] is not None
    assert "health check ok" in row[1]


def test_feedback_omits_the_log_section_when_excerpt_is_empty(client, session_token, monkeypatch):
    captured = {}
    monkeypatch.setattr(
        server_mod.feedback_github, "create_github_issue",
        lambda title, body: captured.update(body=body) or "https://github.com/example/jarvis/issues/1003",
    )
    resp = client.post(
        "/feedback", json={"body": "search feels slow", "severity": "annoying"},
        headers={"Authorization": f"Bearer {session_token}"},
    )

    assert resp.status_code == 200
    assert "<details>" not in captured["body"]
