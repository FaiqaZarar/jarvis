"""Automatic error reporting to the cloud server, which files recurring
errors as GitHub issues - a silent, best-effort side effect alongside whatever error dialog the user already sees, not a
replacement for it.

Phase 1 only: covers errors that happen with a session already
established (e.g. a Google Drive connect failure). An error before
sign-in (daemon-startup failures, sign-in-flow failures) has no session
to report under yet, so this is a no-op for those - a future phase would
need a separate, unauthenticated endpoint with its own abuse/rate-limit
model, not bolted onto this one.
"""

from datetime import datetime, timezone

from jarvis.modules.cloud import client as cloud_client
from jarvis.modules.cloud import session_store
from jarvis.modules.desktop_app import log_excerpt
from jarvis.modules.desktop_app.daemon_worker import run_daemon_call


def report_error(pool, error_type: str, message: str) -> None:
    """Fires the report on a worker thread and never surfaces a result -
    no on_success/on_error given to run_daemon_call, so any failure here
    (network down, server error) is silently dropped rather than showing
    the user a *second* error on top of the one they already see."""
    if not cloud_client.is_configured():
        return
    session = session_store.load_session()
    if session is None:
        return  # nothing to report under yet - see module docstring
    _email, session_token = session

    client_timestamp = datetime.now(timezone.utc).isoformat()
    excerpt = log_excerpt.get_recent_log_excerpt(datetime.now())

    run_daemon_call(
        pool, cloud_client.report_error, session_token, error_type, message, client_timestamp, excerpt,
    )
