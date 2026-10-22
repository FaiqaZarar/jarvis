"""Connector sync - hybrid: incremental (Changes API) when a cursor is
stored, full listing otherwise.
"""

from datetime import datetime, timezone
from typing import Callable, Optional

from jarvis_server.internal import db
from jarvis_server.internal.errors import RateLimitedError
from jarvis_server.internal.logging.logging_config import get_logger
from jarvis_server.connectors import _common
from jarvis_server.connectors.base import Connector
from jarvis_server.connectors.drive._constants import CONNECTOR_NAME, DRIVE_API_NUM_RETRIES, _SHORTCUT
from jarvis_server.connectors.drive.auth import get_drive_service
from jarvis_server.connectors.drive.fetch import is_rate_limited, process_drive_file
from jarvis_server.connectors.drive.listing import list_drive_files_recursive

logger = get_logger(__name__)

_START_PAGE_TOKEN_KEY = "start_page_token"

ProgressCallback = Callable[[int, Optional[int], int], None]


def _require_folder_id(folder_id: Optional[str], fn_name: str) -> None:
    """Shared guard for ``sync_drive`` / ``_sync_drive_full`` / ``DriveConnector``:
    no whole-Drive mode."""
    if not folder_id:
        raise ValueError(
            f"{fn_name}() requires a real Drive folder_id - whole-Drive sync "
            "is not supported. A live test against a real account (2026-09) "
            "found 1,567 files (910 downloadable) with zero scoping; every "
            "sync must be scoped to a specific folder."
        )


def sync_drive(
    conn,
    service,
    folder_id: str,
    connector: str = CONNECTOR_NAME,
    on_progress: Optional[ProgressCallback] = None,
) -> dict:
    """Drive connector sync, run by ``DriveConnector.sync``. Hybrid: with a
    stored Changes API cursor, runs the incremental path (only what
    changed, no fast-skip or sweep needed); otherwise runs the full
    listing path, which mints and persists a fresh cursor.

    ``folder_id`` is REQUIRED - see ``_require_folder_id``. Note it only
    scopes the full-listing path: the Changes API has no per-folder
    concept, so once a cursor exists, incremental runs see changes across
    the whole Drive account regardless (see ``_sync_drive_full``, issue #78).

    ``on_progress(processed, total, errors)`` fires after every file;
    ``total`` is None on the incremental path, which pages through changes
    without knowing how many are left.

    Returns a stats dict: ``{pulled, fast_skipped, skipped, tombstoned, errors}``.
    """
    _require_folder_id(folder_id, "sync_drive")

    stored_token = db.get_connector_state(conn, connector, _START_PAGE_TOKEN_KEY)
    if stored_token is None:
        return _sync_drive_full(conn, service, folder_id, connector, on_progress)

    try:
        return _sync_drive_incremental(conn, service, connector, stored_token, on_progress)
    except Exception:
        # Deliberately broad: googleapiclient re-raises transport failures
        # (ssl.SSLError, ConnectionError, ...) unwrapped, not as HttpError,
        # so an HttpError-only catch would miss them. A full sync is a safe
        # degradation regardless of cause - a real auth/quota/outage
        # problem still fails just as loudly there.
        logger.exception(
            "sync_drive: incremental path failed - discarding the stored cursor "
            "and falling back to a full sync"
        )
        return _sync_drive_full(conn, service, folder_id, connector, on_progress)


def _process_change(conn, service, connector: str, change: dict, now: str, stats: dict) -> None:
    file_id = change["fileId"]
    if change.get("removed") or (change.get("file") or {}).get("trashed"):
        db.tombstone_document_source(conn, connector, file_id)
        stats["tombstoned"] += 1
        return

    file_meta = change["file"]
    try:
        status = process_drive_file(conn, service, file_meta, now=now)
    except Exception as exc:
        logger.exception(
            "sync_drive (incremental): failed to process Drive file %s", file_id
        )
        _common.record_file_error(stats, file_id, file_meta.get("name"), exc)
        return

    if status == "stored":
        stats["pulled"] += 1
    elif status == "skipped":
        stats["skipped"] += 1
    else:  # "failed" or "unreadable" - a real ingest failure, not a skip
        _common.record_file_error(stats, file_id, file_meta.get("name"))


def _sync_drive_incremental(
    conn, service, connector: str, start_page_token: str,
    on_progress: Optional[ProgressCallback] = None,
) -> dict:
    """Changes API delta path: every entry is by definition something that
    changed, so there's no fast-skip, and removals arrive as their own
    ``removed``/``file.trashed`` entries, so there's no sweep call either.
    The new cursor persists only if the run had zero errors; otherwise the
    stored token is left alone so the next run replays this batch."""
    now = datetime.now(timezone.utc).isoformat()
    stats = {"pulled": 0, "fast_skipped": 0, "skipped": 0, "tombstoned": 0, "errors": 0}

    page_token = start_page_token
    new_start_page_token = None
    processed = 0
    while True:
        response = (
            service.changes()
            .list(
                pageToken=page_token,
                spaces="drive",
                # Mirrors list_drive_files's scoping: includeItemsFromAllDrives /
                # supportsAllDrives are deliberately NOT set - setting them
                # only here would leak Shared Drive changes into the incremental
                # path that the full-listing path correctly excludes.
                fields=(
                    "nextPageToken, newStartPageToken, "
                    "changes(fileId, removed, "
                    "file(id, name, mimeType, modifiedTime, shortcutDetails, driveId, trashed))"
                ),
            )
            .execute(num_retries=DRIVE_API_NUM_RETRIES)
        )

        for change in response.get("changes", []):
            processed += 1
            _process_change(conn, service, connector, change, now, stats)
            if on_progress is not None:
                on_progress(processed, None, stats["errors"])

        if response.get("newStartPageToken"):
            new_start_page_token = response["newStartPageToken"]
        page_token = response.get("nextPageToken")
        if not page_token:
            break

    # On error, leave the stored token untouched: process_drive_file is
    # idempotent, so replaying this batch just re-processes successes
    # harmlessly alongside a real retry of the one that failed.
    if stats["errors"] == 0:
        db.set_connector_state(conn, connector, _START_PAGE_TOKEN_KEY, new_start_page_token, now)
    return stats


def _sync_listed_file(
    conn, service, connector: str, file_meta: dict, now: str, stats: dict, stored_modified_ats: dict,
) -> None:
    source_id = file_meta["id"]
    is_shortcut = file_meta.get("mimeType") == _SHORTCUT

    if (
        not is_shortcut
        and stored_modified_ats.get(source_id) == file_meta.get("modifiedTime")
        and source_id in stored_modified_ats
    ):
        db.touch_document_source_seen(conn, connector, source_id, now)
        stats["fast_skipped"] += 1
        stats["pulled"] += 1
        return

    try:
        status = process_drive_file(conn, service, file_meta, now=now)
    except Exception as exc:
        logger.exception("sync_drive: failed to process Drive file %s", source_id)
        _common.record_file_error(stats, source_id, file_meta.get("name"), exc)
        return

    if status == "stored":
        stats["pulled"] += 1
    elif status == "skipped":
        stats["skipped"] += 1
    else:  # "failed" or "unreadable" - a real ingest failure, not a skip
        _common.record_file_error(stats, source_id, file_meta.get("name"))


def _sync_drive_full(
    conn,
    service,
    folder_id: str,
    connector: str = CONNECTOR_NAME,
    on_progress: Optional[ProgressCallback] = None,
) -> dict:
    """Full Drive listing, scoped to ``folder_id`` -> per-file
    fast-skip-or-process. Runs on a true first run or the cursor-rejection
    fallback. On a clean run it mints and persists a fresh Changes API
    cursor so the next run can go incremental; any per-file error skips
    that (same gate the sweep uses), so the next run replays the full path.

    ``folder_id`` is REQUIRED - see ``_require_folder_id``.

    Phase A - walk recursively and, per file, fast-skip or process it:

      * Fast-skip: a non-shortcut file whose listed ``modifiedTime``
        matches the stored value only gets `touch_document_source_seen` -
        no bytes fetched. Shortcuts are excluded: the listing reports the
        shortcut's *own* modifiedTime, but what's stored is the *target's*,
        so comparing them could miss a real edit to the target.
      * No rename detection: a Drive ``fileId`` is stable identity, so a
        renamed file just re-appears under the same ``source_id`` and the
        ``(connector, source_id)`` upsert handles it in place - no
        ``"renamed"`` stat needed.

    Phase B - tombstoning previously-seen sources absent from this pull is
    **disabled while scoped to a folder** (issue #78). ``document_sources``
    doesn't record which folder a file came from, so there's no way to
    tell "vanished from this folder" apart from "just not in this folder" -
    the old unscoped sweep would tombstone every Drive item outside
    ``folder_id`` on every run (issue #61's bug, reintroduced for Drive).

    Returns a stats dict: ``{pulled, fast_skipped, skipped, tombstoned, errors}``.
    ``tombstoned`` is always 0 while the sweep is disabled (issue #78).
    """
    _require_folder_id(folder_id, "_sync_drive_full")

    from googleapiclient.errors import HttpError

    now = datetime.now(timezone.utc).isoformat()
    stats = {"pulled": 0, "fast_skipped": 0, "skipped": 0, "tombstoned": 0, "errors": 0}

    stored_modified_ats = db.get_active_document_source_modified_ats(conn, connector)

    # Unlike everything below (per-file, already caught), a rate-limit
    # here takes the whole run down - translate it into a clear message
    # instead of a raw traceback; nothing was ingested yet.
    try:
        listed_files = list_drive_files_recursive(service, folder_id)
    except HttpError as exc:
        if is_rate_limited(exc):
            raise RateLimitedError(
                "Google is rate-limiting Drive requests - the folder walk "
                "could not complete. Nothing was ingested; wait a minute and "
                "run it again."
            ) from exc
        raise

    for processed, file_meta in enumerate(listed_files, start=1):
        _sync_listed_file(conn, service, connector, file_meta, now, stats, stored_modified_ats)
        if on_progress is not None:
            on_progress(processed, len(listed_files), stats["errors"])

    # Sweep disabled while folder-scoped (see docstring, issue #78):
    # `stored_modified_ats` spans every folder ever synced for this
    # connector, not just `folder_id`, so there's no way to tell "vanished
    # from this folder" apart from "just not in this folder".
    logger.info("sync_drive: tombstone sweep skipped - folder-scoped sync (issue #78)")

    # Gated on errors == 0, same as the sweep: an incomplete run must not
    # advance the cursor, or the next run would go incremental and miss
    # retrying the file that failed. Wrapped in its own try/except so a
    # blip minting the cursor doesn't throw away an otherwise-successful
    # run - it just means the next run is a full sync instead of incremental.
    if stats["errors"] == 0:
        try:
            token = (
                service.changes()
                .getStartPageToken()
                .execute(num_retries=DRIVE_API_NUM_RETRIES)
                .get("startPageToken")
            )
            db.set_connector_state(conn, connector, _START_PAGE_TOKEN_KEY, token, now)
        except Exception:
            logger.exception(
                "sync_drive: full pull succeeded but minting the Changes API cursor "
                "failed - the run's results are kept; the next run does a full sync"
            )

    return stats


def _folder_name(service, folder_id: str) -> Optional[str]:
    """The folder's name in Drive, for the Sources page. Never fails the
    sync it follows: without it the page shows a generic label instead."""
    try:
        request = service.files().get(fileId=folder_id, fields="name")
        return request.execute(num_retries=DRIVE_API_NUM_RETRIES)["name"]
    except Exception:
        logger.warning("sync_drive: couldn't look up the name of folder %s", folder_id, exc_info=True)
        return None


class DriveConnector(Connector):
    """Wraps `sync_drive` for a single Drive folder.

    `folder_id` is validated eagerly, in `__init__` - it's pure validation,
    so failing before any I/O is free. `service` resolution is deliberately
    lazy, deferred to `sync()`: `get_drive_service()` can open a browser for
    OAuth, which must never happen just from constructing this object.
    """

    name = CONNECTOR_NAME

    def __init__(self, folder_id: str, service=None):
        _require_folder_id(folder_id, "DriveConnector")
        self.folder_id = folder_id
        self.service = service

    @property
    def root_id(self) -> str:
        return self.folder_id

    def sync(self, conn, on_progress=None) -> dict:
        service = self.service or get_drive_service()
        stats = sync_drive(
            conn,
            service,
            folder_id=self.folder_id,
            connector=self.name,
            on_progress=on_progress,
        )
        self.display_name = _folder_name(service, self.folder_id)
        return stats

    def owned_source_ids(self, conn, other_root_ids: list[str]) -> set[str]:
        # A Drive file id says nothing about which folder it's in, and this
        # folder's current listing misses files moved or deleted in Drive
        # since they were synced (Drive syncs never sweep them, issue #78).
        # So every active Drive file goes, except those another tracked
        # folder still lists: an incremental sync of that folder only
        # revisits changed files, so it would never bring them back.
        owned = db.get_active_source_ids_for_connector(conn, self.name)
        if other_root_ids:
            service = self.service or get_drive_service()
            for other_root_id in other_root_ids:
                owned -= {entry["id"] for entry in list_drive_files_recursive(service, other_root_id)}
        return owned
