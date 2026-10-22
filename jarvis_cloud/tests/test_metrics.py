"""GET /metrics - the Prometheus scrape target (issue #145)."""


def test_metrics_is_unauthenticated_and_returns_prometheus_text(client):
    resp = client.get("/metrics")
    assert resp.status_code == 200
    assert "text/plain" in resp.headers["content-type"]


def test_metrics_reflects_request_volume(client, email_sender):
    client.get("/health")
    client.get("/health")

    body = client.get("/metrics").text
    assert "http_requests_total" in body


def _logins_total(metrics_text: str) -> float:
    for line in metrics_text.splitlines():
        if line.startswith("logins_total{") or line.startswith("logins_total "):
            return float(line.rsplit(" ", 1)[1])
    return 0.0


def test_login_counter_increments_on_successful_sign_in(client, email_sender):
    from urllib.parse import parse_qs, urlparse

    before = _logins_total(client.get("/metrics").text)

    client.post("/auth/request", json={"email": "user@example.com"})
    _, url = email_sender.sent[0]
    query = parse_qs(urlparse(url).query)
    client.get("/auth/verify", params={"req": query["req"][0], "token": query["token"][0]})
    client.get("/auth/status", params={"request_id": query["req"][0]})

    after = _logins_total(client.get("/metrics").text)
    assert after == before + 1
