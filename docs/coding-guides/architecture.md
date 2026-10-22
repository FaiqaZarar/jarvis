# Connector Layer Architecture

This records the architectural decisions behind the ingestion connector layer (`jarvis/modules/connectors/`) and `jarvis/modules/pipeline.py`, and why they're shaped the way they are. Read this before adding a new connector or changing how `pipeline.run_ingest` works.

## The problem

Before this decision, `pipeline.py` had one `run_X_ingest()` function per connector (`run_filesystem_ingest`, `run_drive_ingest`). Each hand-rolled an identical ~30-line tail - chunk the new documents, embed + index them, decide whether to rebuild the topic hierarchy - because there was no shared seam for "a connector" to plug into. Adding a third connector meant copying that tail a third time.

Separately, `connectors/drive.py` had grown to 885 lines carrying four unrelated concerns (OAuth, listing, fetch/shortcut-resolution, sync orchestration) in one file.

## The `Connector` interface

`jarvis/modules/connectors/base.py` defines the whole seam:

```python
class Connector(ABC):
    name: str

    @abstractmethod
    def sync(self, conn: sqlite3.Connection, on_progress=None) -> dict:
        ...

    def covered_root_ids(self, other_root_ids: list[str]) -> list[str]:
        ...  # default: []

    def owned_source_ids(self, conn: sqlite3.Connection, other_root_ids: list[str]) -> set[str]:
        ...  # default: set()
```

`on_progress(processed, total, errors)` is optional and fires after each item, so the desktop app can show live counts during sync. Pass `total=None` when the connector can't know it up front (Drive's incremental Changes-API path pages without a count).

`covered_root_ids` is a concrete, optional hook (default `[]`): after a clean sync, `pipeline.run_ingest` asks the connector which of the other `root_id`s it already knows about (same `name`) are now fully covered by the one it just synced, and tombstones those `sync_roots` rows (see `docs/coding-guides/database-access.md`). `FilesystemConnector` overrides it with a path-prefix check - syncing a parent folder supersedes an already-tracked subfolder. `DriveConnector` doesn't override it: Drive never stores folder-ancestry data (no `parents` field is fetched anywhere in `connectors/drive/`), so there's nothing to compute it from.

`owned_source_ids` is the second optional hook (default `set()`), used when the user removes a source: `pipeline.remove_source(conn, connector)` asks which active `source_id`s belong to this root and to no other tracked root of the same `name`, then tombstones those and the root's `sync_roots` row in one commit. Nothing is hard-deleted, and syncing the root again brings it all back. `FilesystemConnector` answers with path prefixes. `DriveConnector` can't tell from a Drive id which folder a file is in, and a folder's current listing misses files moved or deleted in Drive since they were synced; so it takes every active Drive file except those another tracked Drive folder still lists through the Drive API.

`pipeline.run_ingest(conn, connector, embedder=None, *, llm_client=None, organize_hierarchy=True, live_graph_path=None)` is the single entrypoint. It knows nothing about `FilesystemConnector` or `DriveConnector` - it only calls `connector.sync(conn)`, then chunks, embeds, and conditionally runs organize/reconcile. A new connector gets all of that for free by implementing `sync()`.

**Why an ABC, not a `Protocol`:** a `Protocol` would be the more idiomatic modern choice for a shape this small, and is the eventual direction (see "Deferred work" below). But this repo has no static type checker (no mypy/pyright config, no type-checking CI) - so a `Protocol`'s usual safety net (a checker catching an incomplete implementation before runtime) doesn't exist here yet. An `ABC` is the only thing that actually enforces the contract today: a subclass missing `sync()` fails at instantiation, not at some later call site. `Embedder` and `LLMClient` are already ABCs, so this also matches the one existing "how do we define an interface" convention in the codebase instead of introducing a second one.

**Constructor injection, not a `sync()` config parameter:** each connector class takes its own connector-specific arguments at construction (`FilesystemConnector(root_path)`, `DriveConnector(folder_id, service=None)`), and `sync(conn)` is the only method the interface requires. This keeps the interface itself trivially small while letting construction be as different as each connector needs.

**No connector-name override:** `FilesystemConnector`/`DriveConnector` hard-code `name = "filesystem"` / `name = "drive"`. The underlying `sync_filesystem`/`sync_drive` functions still accept an optional `connector=` override (unchanged), but nothing constructs the classes with one - a repo-wide check found the override was never exercised with a non-default value anywhere (tests, scripts, or CLI flags), so it wasn't carried forward as speculative flexibility.

**No back-compat wrappers:** `run_filesystem_ingest`/`run_drive_ingest` were deleted outright rather than kept as thin shims. Every caller (`jarvis/cmd/drive_ingest.py`, `scripts/mk_ingest.py`, tests) now constructs a connector and calls `run_ingest` directly.

## `DriveConnector`'s validation timing

```python
class DriveConnector(Connector):
    def __init__(self, folder_id: str, service=None):
        _require_folder_id(folder_id, "DriveConnector")   # eager
        self.folder_id = folder_id
        self.service = service

    def sync(self, conn) -> dict:
        return sync_drive(conn, self.service or get_drive_service(), ...)  # lazy
```

`folder_id` is validated in `__init__` - it's pure validation with no I/O, so failing fast costs nothing. `service` resolution is deliberately deferred to `sync()`: `get_drive_service()` can open a browser for OAuth consent, which must never happen just from constructing the object.

## Package layout

`connectors/filesystem/` and `connectors/drive/` are packages, not single files, split by concern:

- **`filesystem/`** - `walk.py` (discovering eligible files) + `sync.py` (the per-file ingest loop and `FilesystemConnector`).
- **`drive/`** - `_constants.py` (values shared across submodules), `auth.py` (OAuth), `listing.py` (listing files/folders), `fetch.py` (turning one Drive file into bytes, shortcut resolution, staging), `sync.py` (the connector sync + `DriveConnector`).

Each package's `__init__.py` re-exports the same public names the old single file exposed, so external imports (`from jarvis.modules.connectors.drive import sync_drive`, `from jarvis.modules.connectors.filesystem import FilesystemConnector`) are unchanged.

**A subtlety this split surfaces for tests:** a function that calls another function *by bare name from within the same module* resolves that name against its own module's globals - not against whatever a caller re-exported. `tests/test_drive_connector.py` monkeypatches over a dozen internal names (e.g. `datetime`, `fetch_file_bytes`, `_sync_drive_full`, `process_drive_file`) precisely because it's a white-box test suite. After the split, each of those patches has to target the *specific submodule* that actually resolves the name as a bare call, not the `drive` package's `__init__.py` (which only holds a separate re-exported binding). Get this wrong and the patch silently does nothing - the test still passes, but no longer tests what it claims to.

## Adding a new connector

1. Write the connector module (or package, if it's non-trivial).
2. Give it a class implementing `Connector` (`name` + `root_id` + `sync(conn, on_progress=None) -> dict`, plus the optional `covered_root_ids` and `owned_source_ids` hooks above), constructed with whatever arguments it needs. `root_id` is the identifier of whatever root/folder/scope this instance's `sync()` is pointed at (a resolved absolute path for filesystem, a Drive folder id for Drive) - `pipeline.run_ingest` uses it, together with `name`, to record a per-root last-synced-at timestamp in `sync_roots` (see `docs/coding-guides/database-access.md` and `CONTEXT.md`'s "Sync root" entry). A connector with no natural per-sync scope (e.g. a future whole-workspace Notion sync) should return a fixed sentinel string rather than `None` - `sync_roots` and its callers are deliberately null-free.
3. That's it - no changes to `pipeline.py`. A caller does `run_ingest(conn, YourConnector(...))`.

Notion (`connectors/notion.py`) is currently a stub with no `sync()`. When it's implemented for real, it becomes the second data point (after filesystem/Drive) for whether the *inner* sync-loop shape (fast-skip, stats dict, tombstone sweep) also repeats identically across connectors - see "Deferred work."

## Deferred work

- **`Connector` -> `Protocol`, alongside `Embedder`/`LLMClient`.** Planned, but only once a static type checker (mypy or pyright) is adopted - that's what actually gives `Protocol` its safety net. Doing the interface-style migration before the type checker exists would trade a real runtime check (ABC's instantiation-time `TypeError`) for nothing.
- **Generalizing the inner sync loop** (a shared `SourceAdapter`-style driver in `_common.py` for the fast-skip/fetch/store loop itself, not just the chunk/embed/organize tail). Explicitly not done: filesystem's inode-based rename detection and Drive's Changes-API incremental path (no fast-skip, no tombstone sweep) already diverge enough that forcing one generic loop now would trade real duplication for a hook-heavy, shallow abstraction. Revisit once Notion's real `sync()` gives a third data point.
