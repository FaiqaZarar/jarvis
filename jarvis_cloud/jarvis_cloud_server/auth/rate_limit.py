"""Rate-limiting for POST /auth/request - one email address can't be used
to spam another person's inbox with magic-link emails."""

from jarvis_cloud_server.internal import db
from jarvis_cloud_server.internal.db.login_requests import RATE_LIMIT_MAX_REQUESTS


class RateLimited(Exception):
    """Raised by check() - the route handler catches this and returns
    429, same shape as any other typed-exception-to-HTTP-status
    translation in this service."""


def check(conn, email: str) -> None:
    if db.count_recent_for_email(conn, email) >= RATE_LIMIT_MAX_REQUESTS:
        raise RateLimited(f"Too many login requests for {email}")
