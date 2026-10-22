"""Google Drive connector.

Only the genuinely Drive-specific half lives here, split by concern:
`auth.py` (OAuth), `listing.py` (listing My Drive + Shared-with-me),
`fetch.py` (turning one Drive file into bytes on disk, plus shortcut
resolution), and `sync.py` (the connector sync + `DriveConnector`).

Everything after "bytes on disk" - normalize, hash, dedup, the
documents / document_sources upsert - is `connectors/_common.py`, shared
with the filesystem connector.

Identity: ``source_id`` = the Drive ``fileId``; the provenance row's
``mime`` is the *original* Drive mimeType, never the exported format.

**Deliberate, acknowledged exception to server/client isolation**:
`get_drive_service`/`list_drive_folders`/`list_drive_files_recursive`
(auth + listing) are imported directly by `jarvis.cmd.drive_ingest`
(client-side), not routed through the daemon's HTTP API like the actual
ingest call is - interactive OAuth consent has to open a browser and
block in the foreground of whatever process the user is running, which
a background daemon can't do on a caller's behalf. Treat this slice of
the package as public interface client code is expected to depend on
directly, the same way `jarvis.modules.daemon.client` is - not an
accidental leak of server internals. See `jarvis/cmd/drive_ingest.py`'s
own docstring for the full reasoning from that side.
"""

from jarvis_server.connectors import _common
from jarvis_server.connectors.drive._constants import CONNECTOR_NAME, DRIVE_API_NUM_RETRIES
from jarvis_server.connectors.drive.auth import get_drive_service
from jarvis_server.connectors.drive.fetch import (
    INGESTABLE_MIMETYPES,
    STAGING_DIR,
    _is_clean_pdf,
    fetch_file_bytes,
    is_rate_limited,
    process_drive_file,
    resolve_shortcut_target,
)
from jarvis_server.connectors.drive.listing import (
    list_drive_files,
    list_drive_files_recursive,
    list_drive_folders,
)
from jarvis_server.connectors.drive.sync import DriveConnector, _sync_drive_full, sync_drive

__all__ = [
    "CONNECTOR_NAME",
    "DRIVE_API_NUM_RETRIES",
    "DriveConnector",
    "INGESTABLE_MIMETYPES",
    "STAGING_DIR",
    "fetch_file_bytes",
    "get_drive_service",
    "is_rate_limited",
    "list_drive_files",
    "list_drive_files_recursive",
    "list_drive_folders",
    "process_drive_file",
    "resolve_shortcut_target",
    "sync_drive",
]
