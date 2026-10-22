"""get_email_sender()'s provider dispatch, and ResendEmailSender's
request shape / error handling - no DB or TestClient needed, unlike
test_auth.py/test_feedback.py."""

import pytest

from jarvis_cloud_server.auth import email as email_mod


def test_defaults_to_console_sender(monkeypatch):
    monkeypatch.delenv("JARVIS_CLOUD_EMAIL_PROVIDER", raising=False)
    assert isinstance(email_mod.get_email_sender(), email_mod.ConsoleEmailSender)


def test_unknown_provider_raises(monkeypatch):
    monkeypatch.setenv("JARVIS_CLOUD_EMAIL_PROVIDER", "carrier-pigeon")
    with pytest.raises(RuntimeError, match="carrier-pigeon"):
        email_mod.get_email_sender()


def test_resend_requires_an_api_key(monkeypatch):
    monkeypatch.setenv("JARVIS_CLOUD_EMAIL_PROVIDER", "resend")
    monkeypatch.delenv("RESEND_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="RESEND_API_KEY"):
        email_mod.get_email_sender()


def test_resend_sender_posts_the_link_to_the_named_recipient(monkeypatch):
    monkeypatch.setenv("RESEND_API_KEY", "re_test_key")
    calls = []

    class _FakeResponse:
        def raise_for_status(self):
            pass

    monkeypatch.setattr(
        email_mod.requests, "post",
        lambda url, headers, json, timeout: calls.append((url, headers, json, timeout)) or _FakeResponse(),
    )
    sender = email_mod.ResendEmailSender()

    sender.send_magic_link("user@example.com", "https://example.com/auth/verify?req=1&token=abc")

    assert len(calls) == 1
    url, headers, body, timeout = calls[0]
    assert url == "https://api.resend.com/emails"
    assert headers["Authorization"] == "Bearer re_test_key"
    assert body["to"] == ["user@example.com"]
    assert body["from"] == "onboarding@resend.dev"
    assert "https://example.com/auth/verify?req=1&token=abc" in body["html"]


def test_resend_from_email_is_configurable(monkeypatch):
    monkeypatch.setenv("RESEND_API_KEY", "re_test_key")
    monkeypatch.setenv("RESEND_FROM_EMAIL", "sign-in@jarvis.app")
    sender = email_mod.ResendEmailSender()
    assert sender._from_email == "sign-in@jarvis.app"


def test_resend_send_failure_raises_delivery_error(monkeypatch):
    monkeypatch.setenv("RESEND_API_KEY", "re_test_key")

    def _raise(*args, **kwargs):
        raise email_mod.requests.exceptions.ConnectionError("no route to host")

    monkeypatch.setattr(email_mod.requests, "post", _raise)
    sender = email_mod.ResendEmailSender()

    with pytest.raises(email_mod.EmailDeliveryError, match="couldn't be reached"):
        sender.send_magic_link("user@example.com", "https://example.com/verify")


def test_resend_http_error_surfaces_the_providers_message(monkeypatch):
    monkeypatch.setenv("RESEND_API_KEY", "re_test_key")
    response = email_mod.requests.Response()
    response.status_code = 403
    response._content = b'{"statusCode":403,"name":"validation_error","message":"You can only send testing emails to your own email address."}'

    def _post(*args, **kwargs):
        response.raise_for_status()

    monkeypatch.setattr(email_mod.requests, "post", _post)
    sender = email_mod.ResendEmailSender()

    with pytest.raises(email_mod.EmailDeliveryError, match="only send testing emails.*HTTP 403"):
        sender.send_magic_link("someone@example.com", "https://example.com/verify")
