"""The seam every ingestion connector implements.

`pipeline.run_ingest` depends on nothing but this: a `name` for provenance
and logging, and a `sync(conn, on_progress)` call. Everything connector-specific - what
root/folder to read, how to authenticate - is constructor state on the
concrete class, never part of this interface.
"""

import sqlite3
from abc import ABC, abstractmethod
from typing import Callable, Optional


class Connector(ABC):
    """A connector's whole contract: `name`, `root_id`, `sync`, and the
    optional `covered_root_ids` hook below."""

    name: str
    # A human name for this root, for connectors whose root_id isn't one
    # (a Drive folder id). Set by `sync()` when it can look it up; stored
    # in sync_roots so the Sources page can show it.
    display_name: Optional[str] = None

    @property
    @abstractmethod
    def root_id(self) -> str:
        """The identifier of the root/folder this instance is scoped to -
        a resolved absolute path for filesystem, a Drive folder id for
        Drive. Together with `name`, this is what `sync_roots` tracks a
        last-synced-at timestamp against
        (jarvis/internal/db/sync_roots.py). A connector with no natural
        root/folder concept (e.g. a future whole-workspace Notion sync)
        should return a fixed sentinel string rather than None - the
        sync_roots table and its callers are deliberately null-free."""
        raise NotImplementedError

    @abstractmethod
    def sync(
        self,
        conn: sqlite3.Connection,
        on_progress: Optional[Callable[[int, Optional[int], int], None]] = None,
    ) -> dict:
        """Pull whatever changed since the last sync into the store,
        returning connector-specific stats. `on_progress(processed, total,
        errors)`, if given, fires after each item; `total` is None when the
        connector can't know it up front."""
        raise NotImplementedError

    def covered_root_ids(self, other_root_ids: list[str]) -> list[str]:
        """Which of this connector's other already-tracked root_ids are now
        fully covered by this instance's root_id, after this sync
        succeeded - e.g. a previously-synced subfolder once its parent is
        synced. Called by pipeline.run_ingest to decide what to tombstone
        in sync_roots. Default: none - only connectors whose root_id has a
        computable containment relationship (filesystem's path prefix)
        override this."""
        return []

    def owned_source_ids(self, conn: sqlite3.Connection, other_root_ids: list[str]) -> set[str]:
        """The active source_ids under this root that no other tracked root
        (`other_root_ids`, same connector) also covers - what
        `pipeline.remove_source` tombstones when the user removes this root.
        Default: none, so removing such a root only drops it from the
        Sources list."""
        return set()
