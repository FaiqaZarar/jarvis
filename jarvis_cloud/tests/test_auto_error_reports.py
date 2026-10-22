"""POST /errors/report: session validation (same model as /feedback), and
the fingerprint-based dedup that's the whole point of this route - the
same recurring error across many sessions opens exactly one GitHub
issue, not one per occurrence."""

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


def test_errors_report_requires_a_session(client):
    resp = client.post("/errors/report", json={"error_type": "drive_connect_failed", "message": "access_denied"})
    assert resp.status_code == 401


def test_errors_report_rejects_a_bogus_token(client):
    resp = client.post(
        "/errors/report", json={"error_type": "drive_connect_failed", "message": "access_denied"},
        headers={"Authorization": "Bearer not-a-real-session"},
    )
    assert resp.status_code == 401


def test_errors_report_rejects_missing_fields(client, session_token):
    resp = client.post(
        "/errors/report", json={"error_type": "drive_connect_failed"},
        headers={"Authorization": f"Bearer {session_token}"},
    )
    assert resp.status_code == 400


def test_first_occurrence_opens_a_github_issue(client, session_token, monkeypatch):
    captured = {}
    monkeypatch.setattr(
        server_mod.feedback_github, "create_github_issue",
        lambda title, body: captured.update(title=title, body=body) or "https://github.com/example/jarvis/issues/2000",
    )

    resp = client.post(
        "/errors/report",
        json={
            "error_type": "drive_connect_failed", "message": "access_denied: not a test user",
            "client_timestamp": "2026-10-02T00:00:00+00:00", "log_excerpt": "",
        },
        headers={"Authorization": f"Bearer {session_token}"},
    )

    assert resp.status_code == 200
    assert resp.json() == {"reported": True}
    assert captured["title"] == "Auto-reported: drive_connect_failed"
    assert "access_denied: not a test user" in captured["body"]

    with db_connection.get_connection() as conn:
        row = conn.execute(
            "SELECT occurrence_count, github_issue_url FROM auto_error_reports",
        ).fetchone()
    assert row[0] == 1
    assert row[1] == "https://github.com/example/jarvis/issues/2000"


def test_second_occurrence_of_the_same_error_does_not_open_a_second_issue(client, session_token, monkeypatch):
    calls = []
    monkeypatch.setattr(
        server_mod.feedback_github, "create_github_issue",
        lambda title, body: calls.append(1) or "https://github.com/example/jarvis/issues/2001",
    )
    payload = {
        "error_type": "drive_connect_failed", "message": "access_denied: not a test user",
        "client_timestamp": "2026-10-02T00:00:00+00:00", "log_excerpt": "",
    }
    headers = {"Authorization": f"Bearer {session_token}"}

    client.post("/errors/report", json=payload, headers=headers)
    resp = client.post("/errors/report", json=payload, headers=headers)

    assert resp.status_code == 200
    assert len(calls) == 1  # the second call never hit create_github_issue again

    with db_connection.get_connection() as conn:
        row = conn.execute("SELECT occurrence_count FROM auto_error_reports").fetchone()
    assert row[0] == 2


def test_a_different_error_opens_a_separate_issue(client, session_token, monkeypatch):
    calls = []
    monkeypatch.setattr(
        server_mod.feedback_github, "create_github_issue",
        lambda title, body: calls.append(title) or "https://github.com/example/jarvis/issues/2002",
    )
    headers = {"Authorization": f"Bearer {session_token}"}

    client.post(
        "/errors/report",
        json={"error_type": "drive_connect_failed", "message": "access_denied"},
        headers=headers,
    )
    client.post(
        "/errors/report",
        json={"error_type": "ingest_failed", "message": "timeout"},
        headers=headers,
    )

    assert len(calls) == 2

    with db_connection.get_connection() as conn:
        rows = conn.execute("SELECT error_type FROM auto_error_reports ORDER BY error_type").fetchall()
    assert [r[0] for r in rows] == ["drive_connect_failed", "ingest_failed"]


def test_github_failure_still_returns_200_and_does_not_crash(client, session_token, monkeypatch):
    def _boom(title, body):
        raise RuntimeError("GitHub API unreachable")

    monkeypatch.setattr(server_mod.feedback_github, "create_github_issue", _boom)

    resp = client.post(
        "/errors/report",
        json={"error_type": "drive_connect_failed", "message": "access_denied"},
        headers={"Authorization": f"Bearer {session_token}"},
    )

    assert resp.status_code == 200
    assert resp.json() == {"reported": True}

    with db_connection.get_connection() as conn:
        row = conn.execute("SELECT github_issue_url FROM auto_error_reports").fetchone()
    assert row[0] is None  # row exists, just no issue URL since GitHub failed
