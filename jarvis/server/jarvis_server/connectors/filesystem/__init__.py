"""Local filesystem connector.

Reads never happen here directly - they go through jarvis_server.internal.file_handler
and jarvis_server.internal.normalizer.normalize.Normalize. Connector-agnostic sync
logic (hashing, DB upsert, tombstone sweep) lives in _common.py; this package
holds only what's filesystem-specific, split by concern: `walk.py`
(discovering eligible files) and `sync.py` (the per-file ingest loop and
`FilesystemConnector`).
"""

from jarvis_server.connectors.filesystem.sync import FilesystemConnector, sync_filesystem
from jarvis_server.connectors.filesystem.walk import guess_mime, walk_directory

__all__ = ["FilesystemConnector", "sync_filesystem", "guess_mime", "walk_directory"]
