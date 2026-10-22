"""Magic-link email delivery - swappable by config, same convention
`internal/embed/provider.py` (jarvis_server) uses for embedding backends:
one small interface, a dev/test-safe default, a real provider selected by
an env var once one is actually wired up.

`ResendEmailSender` is the first real provider wired up (Resend's API -
one HTTP POST, API-key auth). `ConsoleEmailSender` stays the
default and is still safe to run in production as a no-op-but-logged
fallback if JARVIS_CLOUD_EMAIL_PROVIDER is ever unset.
"""

import os
from abc import ABC, abstractmethod

import requests

from jarvis_cloud_server.internal.logging.logging_config import get_logger

logger = get_logger(__name__)

_RESEND_API_URL = "https://api.resend.com/emails"


class EmailDeliveryError(Exception):
    """The provider refused or couldn't be reached. The message is safe to
    show the end user (it's returned in the /auth/request response)."""


class EmailSender(ABC):
    @abstractmethod
    def send_magic_link(self, email: str, url: str) -> None:
        """Send `url` (the emailed magic link) to `email`. Raises
        EmailDeliveryError if the provider rejects the send or can't be
        reached."""
        raise NotImplementedError


class ConsoleEmailSender(EmailSender):
    """Logs the link instead of emailing it - the default. Useful for
    local dev, and a safe fallback if a real provider isn't configured."""

    def send_magic_link(self, email: str, url: str) -> None:
        logger.info("Magic link for %s: %s", email, url)


class InMemoryEmailSender(EmailSender):
    """Test-only: records every send instead of logging or delivering it,
    so a test can grab the exact link/token it needs to simulate a click
    without scraping log output."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    def send_magic_link(self, email: str, url: str) -> None:
        self.sent.append((email, url))


class ResendEmailSender(EmailSender):
    """Sends via Resend's HTTP API (resend.com) - one POST, API-key auth,
    no SMTP setup. `RESEND_FROM_EMAIL` defaults to Resend's own shared
    `onboarding@resend.dev` sender, which Resend restricts to the
    account owner's own verified address until a real domain is
    verified (resend.com/domains) - any other recipient gets a 403.
    Swap `RESEND_FROM_EMAIL` for an address on a verified domain to
    lift that restriction."""

    def __init__(self) -> None:
        self._api_key = os.environ.get("RESEND_API_KEY")
        if not self._api_key:
            raise RuntimeError(
                "RESEND_API_KEY is not set - create one at resend.com/api-keys "
                "and put it in .env (see .env.example)."
            )
        self._from_email = os.environ.get("RESEND_FROM_EMAIL", "onboarding@resend.dev")

    def send_magic_link(self, email: str, url: str) -> None:
        try:
            response = requests.post(
                _RESEND_API_URL,
                headers={"Authorization": f"Bearer {self._api_key}"},
                json={
                    "from": self._from_email,
                    "to": [email],
                    "subject": "Sign in to Jarvis",
                    "html": (
                        f'<p>Click to sign in: <a href="{url}">{url}</a></p>'
                        "<p>If you didn't request this, you can ignore this email.</p>"
                    ),
                },
                timeout=15,
            )
            response.raise_for_status()
        except requests.HTTPError as exc:
            detail = _resend_error_detail(exc.response)
            logger.error(
                "Resend rejected magic-link email to %s: HTTP %s - %s",
                email, exc.response.status_code, detail,
            )
            raise EmailDeliveryError(f"{detail} (Resend HTTP {exc.response.status_code})") from exc
        except requests.RequestException as exc:
            logger.exception("Failed to reach Resend sending magic-link email to %s", email)
            raise EmailDeliveryError("The email service couldn't be reached.") from exc


def _resend_error_detail(response: requests.Response) -> str:
    """Resend's error bodies are `{"statusCode", "name", "message"}` - the
    message is what says *why* (unverified domain, test-mode recipient
    restriction, invalid address), which `raise_for_status()` drops."""
    try:
        return str(response.json()["message"])
    except (ValueError, KeyError, TypeError):
        return response.text[:200] or response.reason or "unknown error"


def get_email_sender() -> EmailSender:
    provider = os.environ.get("JARVIS_CLOUD_EMAIL_PROVIDER", "console")
    if provider == "console":
        return ConsoleEmailSender()
    if provider == "resend":
        return ResendEmailSender()
    raise RuntimeError(
        f"Unknown JARVIS_CLOUD_EMAIL_PROVIDER={provider!r} - known providers: "
        "'console', 'resend'."
    )
