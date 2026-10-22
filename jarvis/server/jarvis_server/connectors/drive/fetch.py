"""Fetching one Drive file's bytes, resolving shortcuts, and staging +
handing off to the connector-agnostic core.
"""

import json
import os
from pathlib import Path
from typing import Optional

from jarvis_server.internal.logging.logging_config import get_logger
from jarvis_server.connectors import _common
from jarvis_server.connectors.drive._constants import (
    CONNECTOR_NAME,
    DRIVE_API_NUM_RETRIES,
    _SHORTCUT,
)

logger = get_logger(__name__)

_GOOGLE_DOC = "application/vnd.google-apps.document"
_DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
_PDF = "application/pdf"

# The types fetch_file_bytes actually downloads/exports. A shortcut is not
# here because its real target type is only known after resolution.
INGESTABLE_MIMETYPES = frozenset({_GOOGLE_DOC, _DOCX, _PDF})

# Docs export straight to Markdown - an officially supported target for
# application/vnd.google-apps.document. Capped at 10 MB by the API; a Doc
# over that is skipped with a WARNING. The cap can only be detected
# reactively (from the export's 403) since files.list reports no `size`
# for a native Google Doc.
_DOC_EXPORT_MIME = "text/markdown"
_DOC_EXPORT_EXT = ".md"

# Where fetched/exported bytes land before _common picks them up. Named by
# fileId so two files can never collide. gitignored (jarvis_staging/).
#
# JARVIS_DAEMON_HOME override, same precedent as DEFAULT_TOKEN_PATH
# (connectors.drive.auth._default_token_path) / DEFAULT_DB_PATH /
# DEFAULT_GRAPH_PATH: a repo-relative path is meaningless once frozen -
# this one was missed during that migration (real bug: in a packaged
# build it would resolve into PyInstaller's _MEIxxxx temp extraction
# dir, or raise IndexError at import time if parents[5] doesn't exist,
# taking down daemon startup, not just a Drive ingest). Falls back to
# the old repo-relative computation only when JARVIS_DAEMON_HOME is
# unset (dev mode, unpackaged) - matches auth.py's own
# _default_token_path() exactly, including why the fallback's
# parents[5] arithmetic is safe there: every real frozen entrypoint
# (jarvis/cmd/desktop_app.py) sets JARVIS_DAEMON_HOME before importing
# anything that reads it, so that fallback branch only ever runs in dev
# mode, from a real repo checkout, where parents[5] == repo_root
# (verified empirically when this file moved one level deeper during
# the server/client isolation move).
def _default_staging_dir() -> Path:
    if os.environ.get("JARVIS_DAEMON_HOME"):
        return Path(os.environ["JARVIS_DAEMON_HOME"]) / "jarvis_staging"
    return Path(__file__).resolve().parents[5] / "jarvis_staging"


STAGING_DIR = _default_staging_dir()


def _is_export_size_error(exc) -> bool:
    """True for the 403 the Docs export raises when the result would exceed
    the 10 MB cap (``exportSizeLimitExceeded``)."""
    status = getattr(getattr(exc, "resp", None), "status", None)
    if status not in (403, 400):
        return False
    marker = "exportSizeLimitExceeded"
    content = getattr(exc, "content", b"") or b""
    if isinstance(content, bytes):
        content = content.decode("utf-8", "replace")
    return marker in content or marker in str(exc)


def is_rate_limited(exc) -> bool:
    """True for a Google rate-limit response: a 429, or a 403 whose reason
    is one of Drive's quota reasons. googleapiclient already retries these
    up to ``DRIVE_API_NUM_RETRIES`` with backoff; this is only for the
    calls that still surface the error after that budget is spent, so a
    caller can show "try again in a moment" instead of a raw traceback."""
    status = getattr(getattr(exc, "resp", None), "status", None)
    if status == 429:
        return True
    if status != 403:
        return False
    content = getattr(exc, "content", b"") or b""
    if isinstance(content, bytes):
        content = content.decode("utf-8", "replace")
    haystack = content + str(exc)
    return any(
        reason in haystack
        for reason in ("rateLimitExceeded", "userRateLimitExceeded", "dailyLimitExceeded")
    )


def fetch_file_bytes(service, file_meta: dict) -> tuple[Optional[bytes], Optional[str]]:
    """Fetch one Drive file's content.

    Returns ``(data, staged_extension)`` - e.g. ``(b"...", ".md")`` - or
    ``(None, None)`` when the file is deliberately skipped (unsupported
    type, or a Doc export over the 10 MB cap). Never raises for a skip;
    a genuine transport error still propagates.
    """
    from googleapiclient.errors import HttpError

    mime = file_meta.get("mimeType", "")
    file_id = file_meta["id"]
    name = file_meta.get("name", file_id)

    if mime == _GOOGLE_DOC:
        try:
            data = service.files().export_media(
                fileId=file_id, mimeType=_DOC_EXPORT_MIME
            ).execute(num_retries=DRIVE_API_NUM_RETRIES)
        except HttpError as exc:
            if _is_export_size_error(exc):
                logger.warning(
                    "Skipping Google Doc %r (%s): export exceeds the 10 MB API cap",
                    name, file_id,
                )
                return None, None
            raise
        return _as_bytes(data), _DOC_EXPORT_EXT

    if mime in (_DOCX, _PDF):
        data = service.files().get_media(fileId=file_id).execute(num_retries=DRIVE_API_NUM_RETRIES)
        return _as_bytes(data), (".docx" if mime == _DOCX else ".pdf")

    logger.info(
        "Skipping %r (%s): Drive type %s is out of scope (folders, Sheets, "
        "Slides, and unrecognised types are not ingested)",
        name, file_id, mime or "<unknown>",
    )
    return None, None


def _as_bytes(data) -> bytes:
    """``.execute()`` on export_media / get_media returns bytes already;
    normalise the odd str case (some transports) to UTF-8 bytes."""
    return data if isinstance(data, bytes) else str(data).encode("utf-8")


def _is_clean_pdf(data: bytes) -> bool:
    """Placeholder for clean-vs-scanned PDF detection (a PyMuPDF text-layer
    check). Always True for now, and not wired into any control flow yet -
    exists so it's independently testable before the real check lands.

    # TODO (issue #75): jarvis/internal/normalizer/converters/pdf.py's
    # docstring anticipates the same check - implement in one place, not both.
    """
    return True


def resolve_shortcut_target(service, file_meta: dict) -> Optional[dict]:
    """Live-resolve a Drive shortcut to its target's real metadata - never
    trusts ``shortcutDetails.targetMimeType``, a creation-time snapshot.
    Returns a file_meta-shaped dict for the target, or None if it's broken,
    404, resource-key-gated, or chains to another shortcut. A bare 403
    re-raises instead of returning None: it's ambiguous (could be a
    rate-limit, not proof the target is gone), so it must count as an
    error, not a silent skip."""
    from googleapiclient.errors import HttpError

    name = file_meta.get("name")
    shortcut_id = file_meta.get("id")
    details = file_meta.get("shortcutDetails") or {}

    target_id = details.get("targetId")
    if not target_id:
        logger.warning("broken shortcut: %s (%s) has no target", name, shortcut_id)
        return None

    try:
        target = (
            service.files()
            .get(fileId=target_id, fields="id, name, mimeType, modifiedTime")
            .execute(num_retries=DRIVE_API_NUM_RETRIES)
        )
    except HttpError as exc:
        status = getattr(getattr(exc, "resp", None), "status", None)

        if status in (403, 404) and details.get("targetResourceKey"):
            logger.warning(
                "shortcut %s (%s) target requires a resource key, not yet supported",
                name, shortcut_id,
            )
            return None

        if status == 404:
            logger.warning(
                "shortcut %s (%s) points to a vanished or inaccessible target",
                name, shortcut_id,
            )
            return None

        # A bare 403 (no resource key) isn't safe to treat as "gone" - it can
        # be a rate-limit or a real permission change on a file that still
        # exists. Re-raising counts it as an error, which keeps the caller's
        # cursor/retry logic from silently dropping a file that may exist.
        raise

    if target.get("mimeType") == _SHORTCUT:
        logger.warning(
            "shortcut %s (%s) points to another shortcut - chained shortcuts are not resolved",
            name, shortcut_id,
        )
        return None

    return target


def _stage(file_id: str, data: bytes, extension: str) -> Path:
    STAGING_DIR.mkdir(exist_ok=True)
    staged_path = STAGING_DIR / f"{file_id}{extension}"
    staged_path.write_bytes(data)
    return staged_path


def process_drive_file(conn, service, file_meta: dict, *, now: str) -> str:
    """Fetch one Drive file, stage it under ``jarvis_staging/<fileId>``, and
    hand it to `_common.ingest_staged_file` for normalize -> hash -> commit.

    Returns ``"stored"``, ``"skipped"`` (no converter registered for this
    type - expected, not an error), ``"failed"`` (a converter was
    dispatched and its conversion errored - issue #115), or
    ``"unreadable"`` (the staged file itself couldn't be read/opened).
    Staged file is deleted only on ``"stored"``; otherwise it's left for
    the next run.
    """
    # Shortcuts are resolved to their target exactly once, here, before
    # anything else touches the file. fetch_file_bytes and the metadata
    # hand-off both work off the one resolved dict - never two resolve
    # calls for one file. A non-shortcut passes straight through.
    effective_meta = file_meta
    if file_meta.get("mimeType") == _SHORTCUT:
        effective_meta = resolve_shortcut_target(service, file_meta)
        if effective_meta is None:
            return "skipped"

    data, extension = fetch_file_bytes(service, effective_meta)
    if data is None:
        return "skipped"

    # Identity stays with the shortcut itself; only content, mime, and
    # title follow the resolved target.
    file_id = file_meta["id"]
    staged_path = _stage(file_id, data, extension)

    native_metadata_json = None
    if file_meta.get("driveId"):
        native_metadata_json = json.dumps({"shared_drive": True})

    result = _common.ingest_staged_file(
        conn,
        staged_path,
        source_id=file_id,
        connector=CONNECTOR_NAME,
        now=now,
        source_last_seen=now,
        title=effective_meta.get("name"),
        # The ORIGINAL Drive mimeType of the resolved target, not the
        # exported .md/.docx/.pdf (the provenance row describes the
        # source; for a shortcut that's the target's real type, never
        # application/vnd.google-apps.shortcut).
        mime=effective_meta.get("mimeType"),
        source_url=f"https://drive.google.com/open?id={file_id}",
        # Content-recency, so it follows the resolved target too - a
        # shortcut's own modifiedTime is placement metadata, not content.
        source_modified_at=effective_meta.get("modifiedTime"),
        native_metadata_json=native_metadata_json,
    )
    return result.status
