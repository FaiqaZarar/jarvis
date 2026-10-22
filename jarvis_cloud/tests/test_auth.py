"""Magic-link auth flow: request -> verify -> status. Covers the
security-sensitive cases called out in the implementation plan - token
single-use, expiry, rate-limiting, and the session token being handed
back at most once."""

from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

from jarvis_cloud_server import server as server_mod
from jarvis_cloud_server.auth.email import EmailDeliveryError
from jarvis_cloud_server.internal import db
from jarvis_cloud_server.internal.db import connection as db_connection
from jarvis_cloud_server.internal.db.login_requests import RATE_LIMIT_MAX_REQUESTS


def _link_params(url: str) -> tuple[str, str]:
    query = parse_qs(urlparse(url).query)
    return query["req"][0], query["token"][0]


def test_full_sign_in_flow_end_to_end(client, email_sender):
    resp = client.post("/auth/request", json={"email": "user@example.com"})
    assert resp.status_code == 200
    request_id = resp.json()["request_id"]

    assert len(email_sender.sent) == 1
    sent_email, url = email_sender.sent[0]
    assert sent_email == "user@example.com"
    req_param, token = _link_params(url)
    assert req_param == request_id

    # Not confirmed yet.
    status = client.get("/auth/status", params={"request_id": request_id}).json()
    assert status == {"status": "pending"}

    verify = client.get("/auth/verify", params={"req": request_id, "token": token})
    assert verify.status_code == 200
    assert "signed in" in verify.text.lower()

    status = client.get("/auth/status", params={"request_id": request_id}).json()
    assert status["status"] == "confirmed"
    assert "session_token" in status and status["session_token"]

    # The token is only ever handed back once - a second poll must not
    # repeat it.
    status_again = client.get("/auth/status", params={"request_id": request_id}).json()
    assert status_again == {"status": "confirmed"}


def test_email_is_case_insensitive(client, email_sender):
    client.post("/auth/request", json={"email": "User@Example.com"})
    _, url = email_sender.sent[0]
    req_param, token = _link_params(url)
    client.get("/auth/verify", params={"req": req_param, "token": token})

    with db_connection.get_connection() as conn:
        user = db.get_user_by_id(conn, db.get_login_request(conn, req_param)["user_id"])
    assert user["email"].lower() == "user@example.com"


def test_verify_rejects_a_bad_token(client, email_sender):
    resp = client.post("/auth/request", json={"email": "user@example.com"})
    request_id = resp.json()["request_id"]

    verify = client.get("/auth/verify", params={"req": request_id, "token": "not-the-real-token"})
    assert verify.status_code == 400
    assert "close this tab" not in verify.text.lower()

    status = client.get("/auth/status", params={"request_id": request_id}).json()
    assert status == {"status": "pending"}


def test_verify_token_is_single_use(client, email_sender):
    client.post("/auth/request", json={"email": "user@example.com"})
    _, url = email_sender.sent[0]
    req_param, token = _link_params(url)

    first = client.get("/auth/verify", params={"req": req_param, "token": token})
    assert first.status_code == 200

    second = client.get("/auth/verify", params={"req": req_param, "token": token})
    assert second.status_code == 400


def test_verify_rejects_an_expired_token(client, email_sender):
    resp = client.post("/auth/request", json={"email": "user@example.com"})
    request_id = resp.json()["request_id"]
    _, url = email_sender.sent[0]
    _, token = _link_params(url)

    # Force the row into the past instead of waiting 15 real minutes.
    with db_connection.get_connection() as conn:
        conn.execute(
            "UPDATE login_requests SET expires_at = %s WHERE id = %s",
            (datetime.now(timezone.utc) - timedelta(minutes=1), request_id),
        )
        conn.commit()

    verify = client.get("/auth/verify", params={"req": request_id, "token": token})
    assert verify.status_code == 400


def test_rate_limits_repeated_requests_for_one_email(client, email_sender):
    for _ in range(RATE_LIMIT_MAX_REQUESTS):
        resp = client.post("/auth/request", json={"email": "user@example.com"})
        assert resp.status_code == 200

    over_limit = client.post("/auth/request", json={"email": "user@example.com"})
    assert over_limit.status_code == 429

    # A different email is unaffected.
    other = client.post("/auth/request", json={"email": "someone-else@jarvis.app"})
    assert other.status_code == 200


def test_request_rejects_an_invalid_email(client, email_sender):
    resp = client.post("/auth/request", json={"email": "not-an-email"})
    assert resp.status_code == 400
    assert email_sender.sent == []


def test_status_for_unknown_request_id_reads_as_pending(client, email_sender):
    status = client.get("/auth/status", params={"request_id": "00000000-0000-0000-0000-000000000000"})
    assert status.json() == {"status": "pending"}


def test_request_returns_the_email_providers_error(client, monkeypatch):
    class _Failing:
        def send_magic_link(self, email, url):
            raise EmailDeliveryError("domain not verified (Resend HTTP 403)")

    monkeypatch.setattr(server_mod, "_email_sender", _Failing())

    resp = client.post("/auth/request", json={"email": "user@example.com"})

    assert resp.status_code == 502
    assert "domain not verified" in resp.json()["error"]
