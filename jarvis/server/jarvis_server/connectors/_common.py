"""Sync logic shared by every connector, so tombstone scoping and the
normalize/hash/store sequence aren't reimplemented (and drift) per connector.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Optional

from jarvis_server.internal import db
from jarvis_server.internal.errors import ErrorCode, classify
from jarvis_server.internal.file_handler.reader import OpenedFile, open_file
from jarvis_server.internal.normalizer.hashing import hash_content, normalize_to_markdown
from jarvis_server.internal.normalizer.normalize import Normalize
from jarvis_server.internal.logging.logging_config import get_logger

logger = get_logger(__name__)

# Shared across every sync call/connector: Normalize holds no per-call state
# (its converters dict is built once and never mutated), so one instance is
# safe to reuse rather than reconstructing per file.
_NORMALIZER = Normalize()


def record_file_error(
    stats: dict, source_id: str, display_name: Optional[str], exc: Optional[BaseException] = None,
) -> None:
    """Count a file the sync couldn't read, and remember which one and why
    under `stats["failed_files"]` - `pipeline.run_ingest` stores that list
    against the run, on the user's machine only."""
    stats["errors"] += 1
    code = classify(exc) if exc is not None else ErrorCode.UNKNOWN
    if code is ErrorCode.UNKNOWN:
        code = ErrorCode.FILE_READ_FAILED
    stats.setdefault("failed_files", []).append(
        {"source_id": source_id, "display_name": display_name, "error_code": code.value}
    )


def normalize_and_hash(
    opened_file: OpenedFile, *, normalizer: Optional[Normalize] = None
) -> tuple[Optional[str], Optional[str]]:
    """Returns (normalized_markdown, content_hash) on success, never raises.

    On failure, returns (None, reason) - "unsupported" or "failed", per
    `Normalize.normalize`'s contract - one level flatter, since callers
    here only need the category, not the human-readable message."""
    raw_text, _err, reason = (normalizer or _NORMALIZER).normalize(opened_file)
    if raw_text is None:
        return None, reason
    normalized = normalize_to_markdown(raw_text)
    return normalized, hash_content(normalized)


def store_document(
    conn,
    *,
    content_hash: str,
    normalized_markdown: str,
    source_id: str,
    connector: str,
    now: str,
    source_last_seen: str,
    title: Optional[str] = None,
    mime: Optional[str] = None,
    source_url: Optional[str] = None,
    source_modified_at: Optional[str] = None,
    source_mtime: Optional[float] = None,
    source_size: Optional[int] = None,
    source_inode: Optional[str] = None,
    native_metadata_json: Optional[str] = None,
) -> None:
    """`documents` dedups identical content across sources (ON CONFLICT DO
    NOTHING); `document_sources` upserts on (connector, source_id), so a
    re-seen source is refreshed in place and any tombstone is cleared. This
    is the single commit point - a caller that fails before reaching it has
    written nothing.
    """
    db.insert_document(
        conn,
        content_hash=content_hash,
        normalized_markdown=normalized_markdown,
        first_seen_at=now,
        last_ingested_at=now,
    )
    db.insert_document_source(
        conn,
        source_id=source_id,
        connector=connector,
        content_hash=content_hash,
        source_last_seen=source_last_seen,
        source_url=source_url,
        source_modified_at=source_modified_at,
        source_mtime=source_mtime,
        source_size=source_size,
        source_inode=source_inode,
        title=title,
        mime=mime,
        native_metadata_json=native_metadata_json,
    )


@dataclass
class StagedIngestResult:
    status: Literal["stored", "skipped", "failed", "unreadable"]
    content_hash: Optional[str] = None


def ingest_staged_file(
    conn,
    staged_path: Path,
    *,
    source_id: str,
    connector: str,
    now: str,
    source_last_seen: str,
    title: Optional[str] = None,
    mime: Optional[str] = None,
    source_url: Optional[str] = None,
    source_modified_at: Optional[str] = None,
    native_metadata_json: Optional[str] = None,
    normalizer: Optional[Normalize] = None,
) -> StagedIngestResult:
    """Deletes the staged file only after a fully successful commit - any
    failure (bad conversion, unreadable stage, db error) leaves it in place
    so the next run retries it.

    `mime` should be the original source mime (e.g. Drive's mimeType), not
    the staged file's extension - `document_sources.mime` describes the
    source, not the transfer format.
    """
    opened, _open_err = open_file(staged_path)
    if opened is None:
        return StagedIngestResult(status="unreadable")

    normalized, content_hash = normalize_and_hash(opened, normalizer=normalizer)
    if normalized is None:
        return StagedIngestResult(status="skipped" if content_hash == "unsupported" else "failed")

    store_document(
        conn,
        content_hash=content_hash,
        normalized_markdown=normalized,
        source_id=source_id,
        connector=connector,
        now=now,
        source_last_seen=source_last_seen,
        title=title,
        mime=mime,
        source_url=source_url,
        source_modified_at=source_modified_at,
        native_metadata_json=native_metadata_json,
    )

    try:
        staged_path.unlink()
    except OSError:
        logger.warning(
            "Ingested %s but could not delete its staged copy at %s - "
            "it will be re-processed (harmlessly, content-hash dedup) next run",
            source_id, staged_path,
        )
    return StagedIngestResult(status="stored", content_hash=content_hash)


def sweep_vanished_sources(
    conn,
    connector: str,
    *,
    scope_prefix: str,
    seen_source_ids: set[str],
    had_errors: bool,
    scope_label: str = "",
) -> int:
    """Scoped by prefix, **never** globally by connector name: one connector
    may pull several roots/folders, and a sync of one must not tombstone
    another's items just because they weren't in this pull.

    Runs only after a fully clean pull - any error suppresses the sweep
    entirely, so a partial/failed pull never reads as mass deletion.
    """
    if had_errors:
        logger.warning(
            "Skipping tombstone sweep for connector=%s %s - pull had error(s), "
            "and a partial pull must never read as mass deletion",
            connector, scope_label or f"scope_prefix={scope_prefix!r}",
        )
        return 0

    previously_seen = {
        source_id
        for source_id in db.get_active_source_ids_for_connector(conn, connector)
        if source_id.startswith(scope_prefix)
    }
    vanished = previously_seen - seen_source_ids
    for source_id in vanished:
        db.tombstone_document_source(conn, connector, source_id)
    if vanished:
        logger.info(
            "Tombstoned %d source(s) for connector=%s %s",
            len(vanished), connector, scope_label or f"scope_prefix={scope_prefix!r}",
        )
    return len(vanished)
