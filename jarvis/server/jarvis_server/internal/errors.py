"""Turning an exception into an error code (docs/observability-DESIGN.md §6).

One classification serves both outputs: the calm message and fix the
user sees, and the code + phase sent to PostHog. Classification goes by
exception type and HTTP status, never by message text.
"""

import socket
from enum import Enum
from typing import Optional

import requests


class ErrorCode(str, Enum):
    EMBEDDING_KEY_INVALID = "embedding_key_invalid"
    DRIVE_AUTH_EXPIRED = "drive_auth_expired"
    NETWORK_UNAVAILABLE = "network_unavailable"
    RATE_LIMITED = "rate_limited"
    ORGANIZE_FAILED = "organize_failed"
    FILE_READ_FAILED = "file_read_failed"
    UNKNOWN = "unknown"


class EmbeddingAuthError(RuntimeError):
    """The embedding API rejected the key (HTTP 401/403)."""


class DriveAuthRequired(RuntimeError):
    """Google Drive needs the user to sign in again before it can sync."""


class RateLimitedError(RuntimeError):
    """A service kept refusing requests after its own retries ran out.
    Raised where the service signals it in a non-429 way (Drive uses 403)."""


# DESIGN.md §6 message catalogue: what's safe, then what's wrong.
USER_MESSAGES = {
    ErrorCode.EMBEDDING_KEY_INVALID: "Paused. Jarvis needs a valid API key to read your files. Everything already saved is safe.",
    ErrorCode.DRIVE_AUTH_EXPIRED: "Paused. Google Drive needs you to sign in again. Everything already saved is safe.",
    ErrorCode.NETWORK_UNAVAILABLE: "Paused. You're offline. Everything already saved is safe.",
    ErrorCode.RATE_LIMITED: "Paused. A service Jarvis uses asked it to slow down. Everything already saved is safe.",
    ErrorCode.ORGANIZE_FAILED: "Your files are saved and searchable. Jarvis couldn't group them into topics this time.",
    ErrorCode.FILE_READ_FAILED: "Mostly done. Some files couldn't be read. The others are saved.",
    ErrorCode.UNKNOWN: "Something went wrong. Everything already saved is safe.",
}

_ORGANIZE_PHASES = {"organizing", "reconciling"}
_NETWORK_ERRORS = (
    requests.ConnectionError,
    requests.Timeout,
    requests.exceptions.ChunkedEncodingError,
    ConnectionError,
    TimeoutError,
    socket.gaierror,
)


def classify(exc: BaseException, phase: Optional[str] = None) -> ErrorCode:
    """`phase` is the pipeline phase id the failure happened in (see
    `pipeline.PHASE_IDS`). Walks the `raise ... from` chain, since the
    useful type is often the cause rather than the wrapper."""
    for cause in _chain(exc):
        if isinstance(cause, EmbeddingAuthError):
            return ErrorCode.EMBEDDING_KEY_INVALID
        if isinstance(cause, DriveAuthRequired) or type(cause).__name__ == "RefreshError":
            return ErrorCode.DRIVE_AUTH_EXPIRED
        if isinstance(cause, RateLimitedError) or _http_status(cause) == 429:
            return ErrorCode.RATE_LIMITED
        if isinstance(cause, _NETWORK_ERRORS) or type(cause).__name__ == "ServerNotFoundError":
            return ErrorCode.NETWORK_UNAVAILABLE
    if phase in _ORGANIZE_PHASES:
        return ErrorCode.ORGANIZE_FAILED
    return ErrorCode.UNKNOWN


def _chain(exc: BaseException):
    seen = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        yield exc
        exc = exc.__cause__ or exc.__context__


def _http_status(exc: BaseException) -> Optional[int]:
    if isinstance(exc, requests.HTTPError) and exc.response is not None:
        return exc.response.status_code
    # googleapiclient.errors.HttpError, without importing googleapiclient here.
    resp = getattr(exc, "resp", None)
    status = getattr(resp, "status", None)
    return int(status) if status is not None else None
