# J-11 phase 4 — Drive pipeline entrypoint: design

**Status:** findings + proposed defaults, no implementation yet. Same
discipline as J-09/J-10/J-11's identity design doc. Separate PR from the
identity work (PR #68) per the agreed one-PR-per-phase sequencing:
phase 4 (this doc) → phase 2 (Changes API) → phase 3 (retry/backoff).

**Ticket:** J-11 phase 4. Spec source of truth: `mvp-spec.md` §5
(pipeline/re-sync), §8.1 (Drive connector).

---

## 1. Verified current state

### 1a. `pipeline.py`'s chunk/embed/organize stages are already connector-agnostic

`_chunk_unchunked_documents`, `embed_and_index_chunks`, `_hierarchy_rebuild_reason`,
and `_run_phase_b` operate purely on `documents`/`chunks`/`chunk_vectors`/
`topics` — nothing in any of them references the filesystem connector or
any connector-specific concept. `run_filesystem_ingest` is a thin wrapper:
call `sync_filesystem`, then these four shared stages verbatim. The
module's own docstring says as much: *"Connector-agnostic in intent, but
only the filesystem connector exists today."* This is confirmed, not
assumed — a `run_drive_ingest` needs to swap in `sync_drive` and reuse
everything else unchanged.

### 1b. `sync_filesystem`'s exact fast-skip mechanism

One batch read up front (`db.get_active_document_sources(conn,
connector)`, all non-tombstoned rows for the connector), indexed in
memory (`by_source_id`), then per-file: if the freshly-`stat()`'d
`(mtime, size)` matches the stored `(source_mtime, source_size)` exactly,
call `db.touch_document_source_seen(...)` (refreshes `source_last_seen`
only) and skip opening/converting/hashing the file entirely. Only on a
mismatch (or first-time-seen) does it do the real work, and content-hash
dedup downstream is still the actual authority on whether anything
changed.

### 1c. The gap: no reader for `source_modified_at`

`get_active_document_sources`'s query explicitly selects only
`source_mtime, source_size, source_inode` — never `source_modified_at`.
These three are filesystem-only fields (migration `0002`'s own header:
*"only the filesystem connector populates these"*). Nothing today reads
`source_modified_at` back out; `insert_document_source` only ever writes
it. A Drive fast-skip needs exactly this field, and no existing function
returns it.

### 1d. Drive's identity model needs no rename detection at all

`sync_filesystem`'s rename-detection (inode-matched repoint) exists
because a filesystem inode is a rename *signal*, not identity — the path
is identity, and paths change on rename. A Drive `fileId` never changes
when a file is renamed or moved within Drive. There is no Drive analogue
to "a new source_id whose old identity needs repointing" — a renamed
Drive file simply re-appears under the same `source_id` with a new
`name` in the next listing, and the existing upsert-by-`(connector,
source_id)` path already handles that as a normal in-place update. This
is a real simplification, not an oversight to fix later.

---

## 2. Spec-locked — not open for reinterpretation

| # | Locked decision | Source |
|---|---|---|
| 1 | Two-phase: enumerate+ingest (Phase A), then finalize only on a fully successful pull (Phase B). A partial/failed pull never tombstones. | §5.3 |
| 2 | Trigger granularity is connector-level ("re-sync my Drive"), not folder-level or per-document. | §5.2 |
| 3 | Content hash is the authority on whether content changed; a native modified-time/etag is only an optional fast-skip to avoid re-downloading. | §5.2 |
| 4 | Deleted-source lifecycle: tombstone the provenance link, keep `documents`/`chunks` rows for lineage and cheap re-appearance. | §5.2 |
| 5 | Writes are serialized — one re-sync at a time. (Already true of the existing single-`conn` model; nothing new needed for this phase.) | §5.3 |

---

## 3. Genuinely open — decisions needed, proposed defaults in italics

### 3.1 Tombstone scope: unscoped, no folder_id threading — reusing the PR #62 lesson deliberately

*Default:* `sync_drive` takes no folder-scoping parameter and always
lists the whole accessible Drive (`list_drive_files(service)`, no
`folder_id`) before sweeping. Rationale: `sync_filesystem`'s tombstone
sweep is scoped by path prefix specifically because issue #61 showed a
sync of one watched root incorrectly tombstoning another root's files.
Drive's identity (`fileId`) carries no hierarchical relationship to a
folder the way a path does — there is no string-prefix equivalent to
scope by. If `sync_drive` accepted `folder_id` and someone used it for a
real (non-test) run, every previously-seen Drive file outside that
folder would be tombstoned on that run, reintroducing the exact same bug
class in a new connector. `list_drive_files`'s existing `folder_id` param
stays for listing/testing convenience, and is never threaded through a
sweep-capable entrypoint. Matches spec §5.2's own stated granularity
("connector-level," not folder-level).

### 3.2 Fast-skip: `source_modified_at`, exact match, non-shortcuts only

*Default:* one batch read via a new `get_active_document_source_modified_ats`
(§6), indexed by `source_id`. For a non-shortcut file, if the freshly
listed `modifiedTime` exactly matches the stored value, call the
existing (already connector-agnostic) `db.touch_document_source_seen`
and skip fetching bytes entirely. This is spec-sanctioned, not scope
creep — §5.2 names a native modified-time fast-skip as the expected
connector behavior generally, not something invented for this phase.

**Shortcuts are deliberately excluded from the fast-skip.** The listing
gives a shortcut's *own* `modifiedTime`; what's stored from last run is
the *target's* `modifiedTime` (per `J11_DESIGN.md` §3.3's fix). Comparing
those two is comparing unrelated things — it can produce a false "still
unchanged" on a shortcut whose target was genuinely edited, silently
missing a real content change, the failure direction spec cares about
avoiding most. Every shortcut is fully resolved and re-fetched on every
run in this phase. Known, explicit, and bounded — real fixing of this
belongs to phase 2, where the Changes API can report an actual
content-level change on the target, not a per-run guess.

### 3.3 Where the new pieces live

*Default:* `sync_drive(conn, service, connector=CONNECTOR_NAME) -> dict`
lives in `drive.py`, mirroring `sync_filesystem`'s shape and stats-dict
convention. `run_drive_ingest(conn, connector="drive", embedder=None, *,
llm_client=None, organize_hierarchy=True, live_graph_path=None) -> dict`
lives in `pipeline.py`, mirroring `run_filesystem_ingest` — identical
body shape, `sync_drive` substituted for `sync_filesystem`, everything
after that (chunk → embed → organize/reconcile) reused verbatim,
unchanged, per §1a. **`run_drive_ingest` takes no folder parameter at
all** — §3.1 established that a sweep-bearing entrypoint must be
structurally folder-free, not folder-free-by-comment, and both
`run_drive_ingest` and `sync_drive` follow that. `list_drive_files`'s
own `folder_id` stays for listing/testing convenience and is never
threaded up to either entrypoint.

*Honest note: an earlier draft of this section proposed
`run_drive_ingest(conn, root_folder_id=None, …)` "as an optional initial
listing convenience". §3.1's stronger stance overrode it and the
implementation never had that parameter; this paragraph was only brought
in line with the real signature after the adversarial review flagged the
drift, not decided here at design time.*

### 3.4 Stats dict shape

*Default:* `{"pulled": int, "fast_skipped": int, "skipped": int,
"tombstoned": int, "errors": int}` — the same field names as
`sync_filesystem`'s, minus `"renamed"` (§1d: not applicable to Drive at
all, not merely zero).

---

## 4. Out of scope for this phase

- Changes API delta sync — this phase still does a full listing every
  run; Changes API replaces that with real incremental detection (phase
  2).
- Retry/backoff on any Drive API call (phase 3).
- Full resource-key plumbing for shortcuts (already deferred in
  `J11_DESIGN.md` §3.4).
- Any narrowing of the fast-skip gap for shortcuts (§3.2) — phase 2's
  job.

---

## 5. Testing approach

Extends `test_drive_connector.py`'s existing stub pattern.

- A file whose listed `modifiedTime` matches its stored
  `source_modified_at` exactly is fast-skipped: `touch_document_source_seen`
  is called, `fetch_file_bytes`/`process_drive_file` is not.
- A file whose `modifiedTime` differs gets fully processed.
- A first-time-seen file (`source_id` not in the stored index) is fully
  processed regardless of `modifiedTime`.
- A shortcut is fully resolved and re-fetched every run even when its own
  `modifiedTime` is unchanged from the prior run (locks in §3.2's
  deliberate exclusion, so a future change doesn't silently regress it).
- A file present last run and absent from this run's listing is
  tombstoned; one still present is not.
- A run with `errors > 0` skips the tombstone sweep entirely (mirrors
  `sync_filesystem`'s existing partial-pull-never-tombstones behavior).
- `run_drive_ingest`: a thin integration test confirming it calls
  `sync_drive` then the same chunk/embed/organize path
  `run_filesystem_ingest` already exercises — not re-testing
  chunk/embed/organize's own logic, just the wiring.

---

## 6. Implementation shape

New, additive-only `db.py` function (nothing existing changes):

```python
def get_active_document_source_modified_ats(
    conn: sqlite3.Connection, connector: str
) -> dict[str, str]:
    """source_id -> source_modified_at for every non-tombstoned row of a
    connector. Mirrors get_active_document_sources's batch-read pattern,
    for connectors (Drive) whose only native change-detection signal is
    source_modified_at, not the filesystem-specific
    source_mtime/source_size/source_inode fingerprint."""
```

`drive.py`:

```python
def sync_drive(conn, service, connector: str = CONNECTOR_NAME) -> dict:
    """Full Drive listing -> per-file fast-skip-or-process -> tombstone
    sweep. Mirrors sync_filesystem's Phase A/B shape (spec §5.3), with no
    rename detection needed (§1d) and no folder scoping on the sweep
    (§3.1)."""
```

`pipeline.py`:

```python
def run_drive_ingest(
    conn, connector: str = "drive", embedder=None, *,
    llm_client=None, organize_hierarchy: bool = True,
    live_graph_path=None,
) -> dict:
    """Drive equivalent of run_filesystem_ingest: sync_drive (pull +
    normalize + dedup + tombstone) -> the same shared chunk/embed/
    organize+reconcile stages, unchanged."""
```

---

## 7. Summary

- `pipeline.py`'s chunk/embed/organize stages are confirmed
  connector-agnostic already — `run_drive_ingest` reuses them verbatim,
  no new logic needed there.
- Tombstone scope is deliberately unscoped and structurally
  folder-parameter-free, a direct, deliberate application of the PR #62
  lesson to a connector whose identity model can't support the same
  scoping mechanism.
- Fast-skip uses `source_modified_at` (a genuinely new read path, `db.py`
  gains one small function) and deliberately excludes shortcuts, an
  honest, bounded gap rather than a silent correctness risk.
- Drive's `fileId` identity model needs no rename detection at all — a
  real simplification versus the filesystem connector, not a missing
  feature.
