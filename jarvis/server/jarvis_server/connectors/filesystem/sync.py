"""Per-file ingest decisions and the sync loop over one watched root."""

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Literal, Optional

from jarvis_server.internal import db
from jarvis_server.internal.file_handler.reader import open_file
from jarvis_server.internal.logging.logging_config import get_logger
from jarvis_server.connectors import _common
from jarvis_server.connectors.base import Connector
from jarvis_server.connectors.filesystem.walk import guess_mime, walk_directory

logger = get_logger(__name__)


def _fingerprint(file_stat: os.stat_result) -> tuple[float, int, str]:
    # inode_key is a rename *signal* only, never identity - source_id (the
    # absolute path) is what identifies a file.
    return file_stat.st_mtime, file_stat.st_size, f"{file_stat.st_dev}:{file_stat.st_ino}"


_IngestStatus = Literal["fast_skipped", "renamed", "stored", "skipped", "error"]


def _bump_ext_stats(ext_stats: dict[str, dict], extension: str) -> dict:
    return ext_stats.setdefault(
        extension,
        {"supported": True, "tried": 0, "skipped": 0, "failed": 0, "ignored": 0, "bytes": 0},
    )


def _ingest_one_file(
    conn,
    connector: str,
    now: str,
    file_path: Path,
    source_id: str,
    file_stat: os.stat_result,
    by_source_id: dict,
    by_inode: dict,
    claimed_old_ids: set[str],
) -> tuple[_IngestStatus, Optional[str]]:
    """renamed_from is set only when status is "renamed"."""
    mtime, size, inode_key = _fingerprint(file_stat)

    try:
        existing = by_source_id.get(source_id)
        if existing is not None and existing["source_mtime"] == mtime and existing["source_size"] == size:
            db.touch_document_source_seen(conn, connector, source_id, now)
            return "fast_skipped", None

        renamed_from = None
        if existing is None:
            candidate = by_inode.get(inode_key)
            if candidate is not None and candidate["source_id"] not in claimed_old_ids:
                renamed_from = candidate["source_id"]

        opened_file, open_error = open_file(file_path)
        if opened_file is None:
            return "error", None  # open_file already logged why

        normalized, content_hash = _common.normalize_and_hash(opened_file)
        if normalized is None:
            # "unsupported" (no converter for this extension) is a
            # legitimate skip; anything else means a converter was
            # dispatched and failed - a real ingest error, not a skip
            # (issue #115). The converter already logged why.
            return ("skipped" if content_hash == "unsupported" else "error"), None

        modified_at = datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat()
        mime = guess_mime(file_path)

        if renamed_from is not None:
            # Repoint the existing row rather than tombstone + re-insert.
            db.insert_document(
                conn,
                content_hash=content_hash,
                normalized_markdown=normalized,
                first_seen_at=now,
                last_ingested_at=now,
            )
            db.rename_document_source(
                conn,
                connector=connector,
                old_source_id=renamed_from,
                new_source_id=source_id,
                content_hash=content_hash,
                source_last_seen=now,
                source_modified_at=modified_at,
                source_mtime=mtime,
                source_size=size,
                source_inode=inode_key,
                title=file_path.name,
                mime=mime,
            )
            logger.info("Detected rename: %s -> %s", renamed_from, source_id)
            return "renamed", renamed_from

        _common.store_document(
            conn,
            content_hash=content_hash,
            normalized_markdown=normalized,
            source_id=source_id,
            connector=connector,
            now=now,
            source_last_seen=now,
            source_modified_at=modified_at,
            source_mtime=mtime,
            source_size=size,
            source_inode=inode_key,
            title=file_path.name,
            mime=mime,
        )
        return "stored", None
    except Exception:
        # Reads/conversion already return errors as values above; this
        # only catches an unexpected failure from the db.* calls.
        logger.exception("Failed to ingest %s", file_path)
        return "error", None


def _sync_one_listed_file(
    conn,
    connector: str,
    now: str,
    file_path: Path,
    stats: dict,
    ext_stats: dict[str, dict],
    seen_source_ids: set[str],
    by_source_id: dict,
    by_inode: dict,
    claimed_old_ids: set[str],
) -> None:
    source_id = str(file_path.resolve())
    try:
        file_stat = file_path.stat()
    except PermissionError as exc:
        logger.warning(
            "Permission denied reading %s - skipping. On macOS this is usually TCC: "
            "grant Full Disk Access to your terminal/app in System Settings > "
            "Privacy & Security > Full Disk Access, then re-run the sync.",
            file_path,
        )
        _common.record_file_error(stats, source_id, file_path.name, exc)
        return
    except OSError as exc:
        logger.exception("Failed to stat %s", file_path)
        _common.record_file_error(stats, source_id, file_path.name, exc)
        return

    status, renamed_from = _ingest_one_file(
        conn, connector, now, file_path, source_id, file_stat,
        by_source_id, by_inode, claimed_old_ids,
    )

    extension = file_path.suffix.lower()
    if status == "fast_skipped":
        seen_source_ids.add(source_id)
        stats["pulled"] += 1
        stats["fast_skipped"] += 1
    elif status in ("renamed", "stored"):
        seen_source_ids.add(source_id)
        stats["pulled"] += 1
        if status == "renamed":
            stats["renamed"] += 1
            claimed_old_ids.add(renamed_from)
        entry = _bump_ext_stats(ext_stats, extension)
        entry["tried"] += 1
        entry["bytes"] += file_stat.st_size
    elif status == "skipped":
        stats["skipped"] += 1
        _bump_ext_stats(ext_stats, extension)["skipped"] += 1
    else:  # "error"
        _common.record_file_error(stats, source_id, file_path.name)
        _bump_ext_stats(ext_stats, extension)["failed"] += 1


def sync_filesystem(
    conn,
    root_path: str,
    connector: str = "filesystem",
    on_progress: Optional[Callable[[int, Optional[int], int], None]] = None,
) -> dict:
    """`on_progress(processed, total, errors)` fires after every file.

    Tombstoning is scoped to `root`, not the whole connector, and only
    runs after a fully error-free pass - one watched folder's sync can't
    delete another's files, and a partial pull never reads as mass deletion."""
    root = Path(root_path).resolve()
    now = datetime.now(timezone.utc).isoformat()

    seen_source_ids: set[str] = set()
    stats = {
        "pulled": 0, "fast_skipped": 0, "renamed": 0,
        "skipped": 0, "tombstoned": 0, "errors": 0,
    }
    ext_stats: dict[str, dict] = {}

    def _on_unsupported(extension: str) -> None:
        entry = _bump_ext_stats(ext_stats, extension)
        entry["supported"] = False
        entry["ignored"] += 1

    existing_rows = db.get_active_document_sources(conn, connector)
    by_source_id = {row["source_id"]: row for row in existing_rows}
    by_inode = {
        row["source_inode"]: row for row in existing_rows if row["source_inode"] is not None
    }
    # Prevents two different new paths from both claiming the same
    # renamed-from row.
    claimed_old_ids: set[str] = set()

    # Materialized up front so progress has a real denominator - walking is
    # cheap next to reading and converting each file.
    file_paths = list(walk_directory(root, on_unsupported=_on_unsupported))
    for processed, file_path in enumerate(file_paths, start=1):
        _sync_one_listed_file(
            conn, connector, now, file_path, stats, ext_stats, seen_source_ids,
            by_source_id, by_inode, claimed_old_ids,
        )
        if on_progress is not None:
            on_progress(processed, len(file_paths), stats["errors"])

    # Trailing separator so a sibling folder with a shared prefix
    # (…/notes vs …/notes-archive) is never swept.
    stats["tombstoned"] = _common.sweep_vanished_sources(
        conn,
        connector,
        scope_prefix=f"{root}{os.sep}",
        seen_source_ids=seen_source_ids,
        had_errors=stats["errors"] != 0,
        scope_label=f"root={root}",
    )

    logger.info("Synced %s: %s", root, stats)
    stats["ext_stats"] = ext_stats
    return stats


class FilesystemConnector(Connector):
    """Wraps `sync_filesystem` for a single watched root."""

    name = "filesystem"

    def __init__(self, root_path: str):
        self.root_path = root_path

    @property
    def root_id(self) -> str:
        # Resolved, not the raw constructor arg - sync_filesystem() itself
        # resolves before use, and source_id identity is the resolved
        # absolute path (spec §8.2), so root_id matches that convention.
        return str(Path(self.root_path).resolve())

    def sync(self, conn, on_progress=None) -> dict:
        return sync_filesystem(conn, self.root_path, connector=self.name, on_progress=on_progress)

    def covered_root_ids(self, other_root_ids: list[str]) -> list[str]:
        # Same trailing-separator idiom as scope_prefix above, so a sibling
        # folder with a shared prefix (…/notes vs …/notes-archive) is never
        # mistaken for a subfolder.
        prefix = f"{self.root_id}{os.sep}"
        return [root_id for root_id in other_root_ids if root_id.startswith(prefix)]

    def owned_source_ids(self, conn, other_root_ids: list[str]) -> set[str]:
        # Another tracked root can sit inside this one or contain it; either
        # way its files stay, since that root still claims them.
        prefix = f"{self.root_id}{os.sep}"
        other_prefixes = tuple(f"{root_id}{os.sep}" for root_id in other_root_ids)
        return {
            source_id
            for source_id in db.get_active_source_ids_for_connector(conn, self.name)
            if source_id.startswith(prefix) and not source_id.startswith(other_prefixes)
        }
