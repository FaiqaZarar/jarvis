"""OAuth: obtaining an authenticated Drive v3 service.

Not unit-tested directly - the test suite stubs the service object.
"""

import os
import time
from pathlib import Path
from typing import Optional

from jarvis_server.internal.errors import DriveAuthRequired
from jarvis_server.internal.logging.logging_config import get_logger

logger = get_logger(__name__)

SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]

# A token refresh is the one network call in this module that goes over
# `requests` (via google.auth.transport.requests.Request) rather than
# httplib2 - everything else here either does no I/O or goes through
# googleapiclient's own `.execute(num_retries=...)` budget. A transient
# connection drop mid-refresh (confirmed as a real occurrence: a
# ChunkedEncodingError wrapping a ConnectionResetError) is exactly the
# same class of "identical inputs, plain network hiccup" case
# internal/embed/provider.py already retries for the embeddings API -
# same shape here, not shared code, since the two sit in unrelated
# layers (connector vs. internal/embed).
_TOKEN_REFRESH_MAX_RETRIES = 2
_TOKEN_REFRESH_RETRY_DELAY_S = 2.0

# Cached OAuth token (refresh token + last access token). Secret -
# gitignored.
#
# JARVIS_DAEMON_HOME override, same precedent as DEFAULT_DB_PATH
# (jarvis_server.internal.db.connection) / DEFAULT_GRAPH_PATH
# (jarvis_server.internal.graph_db.graph_db): a repo-relative path is
# meaningless once frozen - confirmed the same way env_config.py's own
# docstring already documents for `.env`/EMBEDDING_API_KEY
# (`_spawn_daemon()`'s subprocess cwd doesn't resolve to anything real
# in a packaged build). Falls back to the old repo-relative computation
# only when JARVIS_DAEMON_HOME is unset (dev mode, unpackaged) - the
# repo-root arithmetic below is otherwise unchanged from before this
# fix (`Path(__file__).resolve().parents[5] == repo_root`, verified
# empirically when this file moved one level deeper during the
# server/client isolation move).
def _default_token_path() -> Path:
    if os.environ.get("JARVIS_DAEMON_HOME"):
        return Path(os.environ["JARVIS_DAEMON_HOME"]) / ".google_drive_token.json"
    return Path(__file__).resolve().parents[5] / ".google_drive_token.json"


DEFAULT_TOKEN_PATH = _default_token_path()

# Without this, run_local_server waits forever for the OAuth callback -
# a silent hang if the browser never opens.
_OAUTH_LOCAL_SERVER_TIMEOUT_S = 300


def _client_config_from_env() -> dict:
    """Build the InstalledAppFlow client config from GOOGLE_OAUTH_CLIENT_ID /
    GOOGLE_OAUTH_CLIENT_SECRET (loaded from .env by whichever entrypoint
    runs the ingest)."""
    client_id = os.environ.get("GOOGLE_OAUTH_CLIENT_ID")
    client_secret = os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET")
    if not client_id or not client_secret:
        raise RuntimeError(
            "GOOGLE_OAUTH_CLIENT_ID / GOOGLE_OAUTH_CLIENT_SECRET are not set. "
            "Create an OAuth client (Desktop app) in Google Cloud, add yourself "
            "as a test user on the OAuth consent screen, and put both values in .env."
        )
    return {
        "installed": {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": ["http://127.0.0.1"],
        }
    }


def _refresh_with_retry(creds) -> None:
    """`creds.refresh()` only retries internally for a handful of known-
    transient HTTP statuses (google-auth's own retry logic, scoped to
    the token endpoint's response) - a connection dropping mid-request
    (no response at all) isn't one of them and raises whatever
    `requests` exception the transport saw, unwrapped. Mirrors
    `internal/embed/provider.py`'s `_embed_batch` retry shape for the
    same category of failure; `RefreshError` (a real auth rejection,
    e.g. a revoked or expired refresh token) is never retried - it
    propagates immediately for the caller to treat as "needs
    re-consent"."""
    import requests
    from google.auth.transport.requests import Request

    attempt = 0
    while True:
        try:
            creds.refresh(Request())
            return
        except (requests.ConnectionError, requests.Timeout, requests.exceptions.ChunkedEncodingError) as exc:
            attempt += 1
            if attempt > _TOKEN_REFRESH_MAX_RETRIES:
                logger.exception(
                    "Drive token refresh failed after %d attempt(s) (transient network "
                    "error, giving up)", attempt,
                )
                raise
            logger.warning(
                "Drive token refresh hit a transient network error (%s) - retrying "
                "(attempt %d/%d)", type(exc).__name__, attempt, _TOKEN_REFRESH_MAX_RETRIES,
            )
            time.sleep(_TOKEN_REFRESH_RETRY_DELAY_S * attempt)


def get_drive_service(
    token_path: Path = DEFAULT_TOKEN_PATH,
    *,
    allow_interactive_auth: Optional[bool] = None,
):
    """Return an authenticated Drive v3 service, refreshing or re-consenting
    as needed.

    ``allow_interactive_auth`` guards the browser fallback, which used to
    hang a headless caller forever: ``None`` (default) decides by
    ``sys.stdin.isatty()`` - a real terminal gets the browser flow, a
    non-interactive process fails fast instead. ``True``/``False`` forces
    it either way.
    """
    import sys

    from google.auth.exceptions import RefreshError
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    had_cached_token = token_path.exists()
    creds: Optional[Credentials] = None
    if had_cached_token:
        creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)

    if creds and creds.expired and creds.refresh_token:
        try:
            _refresh_with_retry(creds)
        except RefreshError:
            logger.info("Cached Drive token could not be refreshed - re-consent needed")
            creds = None

    if not creds or not creds.valid:
        interactive = (
            sys.stdin.isatty() if allow_interactive_auth is None else allow_interactive_auth
        )
        reauth = had_cached_token  # a token file was there but is unusable

        if not interactive:
            what = (
                "re-authorization (the ~weekly Testing-mode token has expired)"
                if reauth
                else "first-time authorization"
            )
            raise DriveAuthRequired(
                f"Google Drive needs {what}, which requires a browser and an "
                "interactive terminal. Run `python -m jarvis.cmd.drive_ingest` from a "
                "normal terminal to sign in, then re-run this."
            )

        if reauth:
            print(
                "\nYour Google Drive authorization has expired (Testing-mode "
                "tokens last ~7 days).\nA browser window will open for you to "
                "sign in again.\n"
            )
        else:
            print(
                "\nJarvis needs permission to read your Google Drive.\n"
                "A browser window will open for you to sign in.\n"
            )

        flow = InstalledAppFlow.from_client_config(_client_config_from_env(), SCOPES)
        # host="127.0.0.1" (not "localhost"): newer Google OAuth clients
        # reject the literal string "localhost" in the redirect URI with an
        # opaque 400. port=0 -> let the OS pick a free port.
        creds = flow.run_local_server(
            host="127.0.0.1", port=0, timeout_seconds=_OAUTH_LOCAL_SERVER_TIMEOUT_S
        )
        token_path.write_text(creds.to_json())
        # Best-effort defense-in-depth (the containing app-data dir is
        # already private on both target platforms) - same rationale as
        # env_config.save_settings's identical try/chmod/except.
        try:
            os.chmod(token_path, 0o600)
        except OSError:
            pass
        logger.info("Drive OAuth complete - token cached at %s", token_path)

    return build("drive", "v3", credentials=creds, cache_discovery=False)
