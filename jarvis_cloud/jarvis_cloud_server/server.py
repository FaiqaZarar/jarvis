"""Jarvis's shared cloud server (GitHub issue #145): magic-link sign-in,
feedback collection (opens a GitHub issue per report), and metrics.

Route handlers do the same "thin routing layer, real work via
asyncio.to_thread" job jarvis_server's own daemon/server.py does for its
(SQLite) queries - see that module for the convention this mirrors.

Unlike that daemon (loopback-only, one process per user), this is a
public, multi-user server: it binds a real host/port (not an
OS-assigned loopback socket) and has no TrustedHost/Origin loopback
defenses, since there's no localhost boundary to defend here.
"""

import asyncio
import hashlib
import os

from dotenv import load_dotenv

load_dotenv()

import uvicorn  # noqa: E402
from starlette.applications import Starlette  # noqa: E402
from starlette.middleware import Middleware  # noqa: E402
from starlette.responses import HTMLResponse, JSONResponse  # noqa: E402
from starlette.routing import Route  # noqa: E402

from jarvis_cloud_server.auth import rate_limit, tokens  # noqa: E402
from jarvis_cloud_server.auth.email import EmailDeliveryError, get_email_sender  # noqa: E402
from jarvis_cloud_server.auth.rate_limit import RateLimited  # noqa: E402
from jarvis_cloud_server.feedback import github as feedback_github  # noqa: E402
from jarvis_cloud_server.internal import db  # noqa: E402
from jarvis_cloud_server.internal.logging.logging_config import get_logger  # noqa: E402
from jarvis_cloud_server.metrics import MetricsMiddleware, login_counter, metrics_endpoint  # noqa: E402

logger = get_logger(__name__)

# The base URL this process's own /auth/verify is reachable at, used to
# build the link that goes in the email - e.g. https://api.jarvis.app.
# Deliberately separate from any per-request Host header (which an
# untrusted client controls) - see .env.example.
PUBLIC_BASE_URL = os.environ.get("JARVIS_CLOUD_PUBLIC_BASE_URL", "http://127.0.0.1:8000")

# Computed once at import time, same as jarvis_server's own module-level
# singletons (e.g. daemon/server.py's _registered_clients) - tests
# monkeypatch this name directly to swap in InMemoryEmailSender.
_email_sender = get_email_sender()


async def health(request):
    return JSONResponse({"ok": True})


def _create_login_request(email: str) -> str:
    with db.get_connection() as conn:
        rate_limit.check(conn, email)
        token = tokens.generate_token()
        token_hash = tokens.hash_token(token)
        request_id = db.create_login_request(conn, email, token_hash)
    url = f"{PUBLIC_BASE_URL}/auth/verify?req={request_id}&token={token}"
    _email_sender.send_magic_link(email, url)
    return request_id


async def api_auth_request(request):
    body = await request.json()
    email = str(body.get("email", "")).strip().lower()
    if not email or "@" not in email:
        return JSONResponse({"error": "A valid email is required."}, status_code=400)
    try:
        request_id = await asyncio.to_thread(_create_login_request, email)
    except RateLimited:
        return JSONResponse(
            {"error": "Too many login attempts for this email - try again later."},
            status_code=429,
        )
    except EmailDeliveryError as exc:
        return JSONResponse(
            {"error": f"Couldn't send the sign-in email: {exc}"}, status_code=502,
        )
    return JSONResponse({"request_id": request_id})


def _check_status(request_id: str) -> dict:
    with db.get_connection() as conn:
        row = db.get_login_request(conn, request_id)
        if row is None or row["confirmed_at"] is None:
            # Same response whether the request_id is unknown, not yet
            # confirmed, or expired - never leak which case it is.
            return {"status": "pending"}

        user_id = db.claim_session_delivery(conn, request_id)
        if user_id is None:
            # Already confirmed, but this poll didn't win the one-time
            # delivery claim (a previous poll already did).
            return {"status": "confirmed"}

        token = tokens.generate_token()
        token_hash = tokens.hash_token(token)
        db.create_session(conn, user_id, token_hash)

    login_counter.add(1)
    return {"status": "confirmed", "session_token": token}


async def api_auth_status(request):
    request_id = request.query_params.get("request_id")
    if not request_id:
        return JSONResponse({"error": "request_id is required"}, status_code=400)
    result = await asyncio.to_thread(_check_status, request_id)
    return JSONResponse(result)


_VERIFY_SUCCESS_HTML = """<!doctype html><html><body style="font-family:sans-serif;text-align:center;padding:64px">
<h2>You're signed in</h2><p>You can close this tab and go back to Jarvis.</p></body></html>"""

_VERIFY_FAILURE_HTML = """<!doctype html><html><body style="font-family:sans-serif;text-align:center;padding:64px">
<h2>This link isn't valid</h2><p>It may have already been used, or it's expired - go back to Jarvis and request a new one.</p></body></html>"""


def _verify(request_id: str, token: str) -> bool:
    token_hash = tokens.hash_token(token)
    with db.get_connection() as conn:
        email = db.confirm_and_consume(conn, request_id, token_hash)
        if email is None:
            return False
        user_id = db.get_or_create_user(conn, email)
        db.set_user_id(conn, request_id, user_id)
    return True


async def api_auth_verify(request):
    request_id = request.query_params.get("req")
    token = request.query_params.get("token")
    if not request_id or not token:
        return HTMLResponse(_VERIFY_FAILURE_HTML, status_code=400)
    ok = await asyncio.to_thread(_verify, request_id, token)
    if not ok:
        return HTMLResponse(_VERIFY_FAILURE_HTML, status_code=400)
    return HTMLResponse(_VERIFY_SUCCESS_HTML)


# Defense in depth against an oversized payload - the client already
# bounds its own excerpt (log_excerpt.get_recent_log_excerpt's max_chars),
# but the server never trusts a client-side limit alone.
_MAX_LOG_EXCERPT_CHARS = 20000


def _log_excerpt_block(log_excerpt: str) -> str:
    """The collapsible `<details>` section both /feedback and
    /errors/report append to a GitHub issue body - factored out so the
    two routes' issue formatting can't drift apart."""
    if not log_excerpt:
        return ""
    return (
        f"\n\n<details>\n<summary>Recent client log (~2 min before report)</summary>\n\n"
        f"```\n{log_excerpt}\n```\n</details>"
    )


def _submit_feedback(
    session_token: str, feedback_body: str, severity: str, client_timestamp: str, log_excerpt: str,
) -> tuple[int, dict]:
    token_hash = tokens.hash_token(session_token)
    with db.get_connection() as conn:
        session = db.get_active_session_by_token_hash(conn, token_hash)
        if session is None:
            return 401, {"error": "Sign in again - this session is no longer valid."}

        log_excerpt = log_excerpt[-_MAX_LOG_EXCERPT_CHARS:]

        # Submitter's email in the issue body - an explicit, deliberate
        # decision (not the default), confirmed with the repo owner
        # given the privacy trade-off of putting a real email address
        # into a third-party issue tracker.
        user = db.get_user_by_id(conn, session["user_id"])
        issue_body = (
            f"**From:** {user['email']}\n**Severity:** {severity}\n"
            f"**Reported at:** {client_timestamp}\n\n{feedback_body}"
        )
        issue_body += _log_excerpt_block(log_excerpt)
        try:
            issue_url = feedback_github.create_github_issue(
                title="Feedback from Jarvis", body=issue_body,
            )
        except Exception:
            logger.exception("Failed to open a GitHub issue for feedback")
            db.insert_feedback(conn, session["user_id"], feedback_body, severity, None, client_timestamp, log_excerpt)
            return 502, {"error": "Feedback was saved, but opening a GitHub issue failed."}

        db.insert_feedback(conn, session["user_id"], feedback_body, severity, issue_url, client_timestamp, log_excerpt)
    return 200, {"issue_url": issue_url}


async def api_feedback(request):
    auth_header = request.headers.get("authorization", "")
    if not auth_header.lower().startswith("bearer "):
        return JSONResponse({"error": "Missing Authorization: Bearer <token> header."}, status_code=401)
    session_token = auth_header[len("bearer "):].strip()

    body = await request.json()
    feedback_body = str(body.get("body", "")).strip()
    if not feedback_body:
        return JSONResponse({"error": "body is required"}, status_code=400)
    severity = str(body.get("severity", "")).strip().lower()
    if severity not in db.SEVERITIES:
        return JSONResponse(
            {"error": f"severity must be one of {db.SEVERITIES}"}, status_code=400,
        )
    # Both optional - an older client that hasn't picked up this change
    # yet must never 400 just for omitting them.
    client_timestamp = str(body.get("client_timestamp", ""))
    log_excerpt = str(body.get("log_excerpt", ""))

    status_code, payload = await asyncio.to_thread(
        _submit_feedback, session_token, feedback_body, severity, client_timestamp, log_excerpt,
    )
    return JSONResponse(payload, status_code=status_code)


def _fingerprint(error_type: str, message: str) -> str:
    """Global dedup key (migration 0004's own comment on why this is
    global, not per-user) - computed server-side, never trusting a
    client-supplied dedup key."""
    return hashlib.sha256(f"{error_type}:{message}".encode("utf-8")).hexdigest()


def _report_error(
    session_token: str, error_type: str, message: str, client_timestamp: str, log_excerpt: str,
) -> tuple[int, dict]:
    token_hash = tokens.hash_token(session_token)
    with db.get_connection() as conn:
        session = db.get_active_session_by_token_hash(conn, token_hash)
        if session is None:
            return 401, {"error": "Sign in again - this session is no longer valid."}

        log_excerpt = log_excerpt[-_MAX_LOG_EXCERPT_CHARS:]
        fingerprint = _fingerprint(error_type, message)
        is_first_occurrence, existing_issue_url = db.record_error_report(conn, fingerprint, error_type, message)

        if not is_first_occurrence:
            # Already reported - the INSERT ... ON CONFLICT above already
            # bumped occurrence_count/last_seen_at, nothing else to do.
            return 200, {"reported": True}

        issue_body = (
            f"**Error type:** {error_type}\n**Reported at:** {client_timestamp}\n\n{message}"
        )
        issue_body += _log_excerpt_block(log_excerpt)
        try:
            issue_url = feedback_github.create_github_issue(
                title=f"Auto-reported: {error_type}", body=issue_body,
            )
        except Exception:
            logger.exception("Failed to open a GitHub issue for an auto-reported error")
            return 200, {"reported": True}  # the row is saved either way - fire-and-forget to the client

        db.set_github_issue_url(conn, fingerprint, issue_url)
    return 200, {"reported": True}


async def api_errors_report(request):
    # Session-gated like /feedback - this phase only covers errors that
    # happen with a session already established (see the plan's own
    # "explicitly out of scope" note on pre-session errors).
    auth_header = request.headers.get("authorization", "")
    if not auth_header.lower().startswith("bearer "):
        return JSONResponse({"error": "Missing Authorization: Bearer <token> header."}, status_code=401)
    session_token = auth_header[len("bearer "):].strip()

    body = await request.json()
    error_type = str(body.get("error_type", "")).strip()
    message = str(body.get("message", "")).strip()
    if not error_type or not message:
        return JSONResponse({"error": "error_type and message are required"}, status_code=400)
    client_timestamp = str(body.get("client_timestamp", ""))
    log_excerpt = str(body.get("log_excerpt", ""))

    status_code, payload = await asyncio.to_thread(
        _report_error, session_token, error_type, message, client_timestamp, log_excerpt,
    )
    return JSONResponse(payload, status_code=status_code)


app = Starlette(
    routes=[
        Route("/health", health),
        Route("/auth/request", api_auth_request, methods=["POST"]),
        Route("/auth/status", api_auth_status, methods=["GET"]),
        Route("/auth/verify", api_auth_verify, methods=["GET"]),
        Route("/feedback", api_feedback, methods=["POST"]),
        Route("/errors/report", api_errors_report, methods=["POST"]),
        Route("/metrics", metrics_endpoint, methods=["GET"]),
    ],
    middleware=[
        Middleware(MetricsMiddleware),
    ],
)


def main() -> None:
    port = int(os.environ.get("PORT", "8000"))
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")


if __name__ == "__main__":
    main()
