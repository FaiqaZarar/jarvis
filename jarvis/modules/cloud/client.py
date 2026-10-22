"""Client side of the shared cloud server (jarvis_cloud_server - GitHub
issue #145): magic-link sign-in and feedback submission.

Mirrors `jarvis.modules.daemon.client`'s exact shape (same `requests`-based
dispatch, retry-once-on-transient-failure convention) with one real
difference: the cloud server has no local auto-start (it's a remote,
already-running service, not something this process spawns), and its
error responses are a flat `{"error": "message"}` string, not the local
daemon's nested `{"error": {"code","message"}}` MCP envelope. Most non-2xx
responses here (401 bad/expired session, 429 rate-limited, 400 bad input,
502 GitHub-side failure) are legitimate application responses a caller
needs to branch on - not a sign the server is broken - so `call()` returns
them as data (status code + body) instead of raising. It only raises for
genuine unreachability: connection refused/reset, a timeout, or a
non-JSON body.
"""

import os
import time
from typing import Optional

import requests

from jarvis_server.internal._bundled_credentials import BUNDLED_CLOUD_BASE_URL
from jarvis_server.internal.logging.logging_config import get_logger

logger = get_logger(__name__)

# Official builds get their server's address at build time (build.yml);
# JARVIS_CLOUD_BASE_URL overrides it, for running your own jarvis_cloud.
# A build from source has neither, and then sign-in, feedback and error
# reports are simply off - see is_configured().
_BASE_URL = (os.environ.get("JARVIS_CLOUD_BASE_URL") or BUNDLED_CLOUD_BASE_URL).rstrip("/")

_RETRY_DELAY_S = 1.5


def is_configured() -> bool:
    """Whether this build talks to a cloud server at all. Callers hide
    sign-in, feedback and error reporting when it doesn't."""
    return bool(_BASE_URL)

_DEFAULT_TIMEOUT_S = 15.0


class CloudError(Exception):
    """Base for every cloud-server-communication failure. Never raised
    directly - always one of the two tiers below."""


class CloudConnectionError(CloudError):
    """The request failed outright - connection refused/reset, or a
    non-JSON response - even after the one retry."""


class CloudTimeoutError(CloudError):
    """Same situation as CloudConnectionError, but the failure was
    specifically a timeout, even after the one retry."""


_USER_MESSAGES = {
    CloudConnectionError: "Can't reach Jarvis's cloud service right now. Try again in a moment.",
    CloudTimeoutError: "Jarvis's cloud service isn't responding. Try again in a moment.",
}


def _do_request(
    method: str, url: str, json_body: Optional[dict], timeout: float,
    headers: Optional[dict] = None,
) -> tuple[int, dict]:
    resp = requests.request(method, url, json=json_body, timeout=timeout, headers=headers)
    body = resp.json()  # a non-JSON body raises ValueError here, inside the retry logic below
    return resp.status_code, body


def call(
    method: str,
    path: str,
    json_body: Optional[dict] = None,
    timeout: float = _DEFAULT_TIMEOUT_S,
    retry_on_timeout: bool = True,
    headers: Optional[dict] = None,
) -> tuple[int, dict]:
    """Make one request against the cloud server. Returns
    (status_code, body) for any completed exchange, whatever the status -
    an application-level error (401/429/400/502) is still a real,
    well-formed JSON response for the caller to branch on, not a reason
    to raise. Only raises CloudConnectionError/CloudTimeoutError for
    genuine unreachability, after one automatic retry - same convention
    as jarvis.modules.daemon.client.call()."""
    url = f"{_BASE_URL}{path}"

    try:
        return _do_request(method, url, json_body, timeout, headers)
    except requests.exceptions.Timeout as exc:
        if not retry_on_timeout:
            logger.exception("cloud request timed out (%s %s) - not retrying", method, path)
            raise CloudTimeoutError(_USER_MESSAGES[CloudTimeoutError]) from exc
        logger.warning("cloud request timed out (%s %s) - retrying once", method, path)
        time.sleep(_RETRY_DELAY_S)
        try:
            return _do_request(method, url, json_body, timeout, headers)
        except requests.exceptions.Timeout as exc2:
            logger.exception("cloud request timed out again after one retry (%s %s)", method, path)
            raise CloudTimeoutError(_USER_MESSAGES[CloudTimeoutError]) from exc2
        except (requests.exceptions.RequestException, ValueError) as exc2:
            logger.exception("cloud request failed on retry (%s %s)", method, path)
            raise CloudConnectionError(_USER_MESSAGES[CloudConnectionError]) from exc2
    except (requests.exceptions.RequestException, ValueError):
        logger.warning("cloud request failed (%s %s) - retrying once", method, path)
        time.sleep(_RETRY_DELAY_S)
        try:
            return _do_request(method, url, json_body, timeout, headers)
        except requests.exceptions.Timeout as exc:
            logger.exception("cloud request timed out on retry (%s %s)", method, path)
            raise CloudTimeoutError(_USER_MESSAGES[CloudTimeoutError]) from exc
        except (requests.exceptions.RequestException, ValueError) as exc:
            logger.exception("cloud request failed again after one retry (%s %s)", method, path)
            raise CloudConnectionError(_USER_MESSAGES[CloudConnectionError]) from exc


# ---------------------------------------------------------------------
# Typed wrappers - thin, just fix the path/method. Callers branch on the
# returned status code (see each endpoint's own shape in
# jarvis_cloud_server/server.py) rather than exceptions for anything
# except CloudError itself (server unreachable).
# ---------------------------------------------------------------------


def request_magic_link(email: str) -> tuple[int, dict]:
    return call("POST", "/auth/request", {"email": email})


def poll_login_status(request_id: str) -> tuple[int, dict]:
    return call("GET", f"/auth/status?request_id={request_id}")


def submit_feedback(
    session_token: str, body: str, severity: str, client_timestamp: str, log_excerpt: str,
) -> tuple[int, dict]:
    """`severity` must be one of "minor"/"annoying"/"blocking" - the server
    rejects anything else with a 400 (issue #145's Figma mockup - kept in
    sync with jarvis_cloud_server.internal.db.feedback.SEVERITIES).
    `client_timestamp` (UTC ISO 8601) and `log_excerpt` (the preceding
    ~2 minutes of the local daemon log, or "" if unavailable) give a
    vague "it's not working" report actual debugging context - see
    jarvis.modules.desktop_app.log_excerpt."""
    return call(
        "POST", "/feedback",
        {
            "body": body, "severity": severity,
            "client_timestamp": client_timestamp, "log_excerpt": log_excerpt,
        },
        headers={"Authorization": f"Bearer {session_token}"},
    )


def report_error(
    session_token: str, error_type: str, message: str, client_timestamp: str, log_excerpt: str,
) -> tuple[int, dict]:
    """Automatic error reporting (not a user-submitted feedback report) -
    see jarvis.modules.desktop_app.error_reporting. The server dedupes by
    a fingerprint of (error_type, message) - the same recurring error
    across many sessions opens one GitHub issue, not one per occurrence."""
    return call(
        "POST", "/errors/report",
        {
            "error_type": error_type, "message": message,
            "client_timestamp": client_timestamp, "log_excerpt": log_excerpt,
        },
        headers={"Authorization": f"Bearer {session_token}"},
    )
