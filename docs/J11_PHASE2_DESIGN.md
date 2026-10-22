# J-11 phase 2 — Drive Changes API incremental sync: design

**Status:** findings + proposed defaults, no implementation yet. Same
discipline as the rest of J-11's design docs. Sequenced after phase 4
(PR #72): this enhances `sync_drive` in place rather than replacing it.

**Ticket:** J-11 phase 2. Spec source of truth: `mvp-spec.md` §5.2
(delta detection), §8.1 (Drive connector).

---

## 1. Verified current state

### 1a. Drive API v3 Changes mechanics, verified against Google's own docs, not assumed

- `changes.getStartPageToken()` → `{startPageToken: "..."}` — the initial
  cursor, called once when no stored cursor exists yet.
- `changes.list(pageToken=..., fields=...)` — paginated via
  `nextPageToken` exactly like `files.list` already is in
  `list_drive_files`. Only the **final** page's response includes
  `newStartPageToken` — that's the cursor to persist for the *next* run,
  not the one just used.
- Each `Change` resource (v3): `fileId`, `removed` (boolean — true means
  permanently gone or access lost, no `file` object present), `file`
  (the updated File resource, present when not removed).
- Google's own guide: *"If the [page] token is rejected for any reason,
  it should be discarded, and pagination should be restarted from the
  first page of results."* In practice this means: a stored cursor can
  go stale (expire, or become invalid for reasons outside this
  connector's control), and the documented, correct response is to fall
  back to a full sync and mint a fresh cursor — not treat it as fatal.

### 1b. `trashed` is a distinct signal from `removed`, both mean "gone"

A file moved to trash (not permanently deleted) appears in the changes
feed as a normal update with `file.trashed == true`, not as a `removed`
entry. `list_drive_files`'s existing full listing already filters
`trashed = false` server-side, so a trashed file simply doesn't appear
there. The incremental path sees changes as they happen, so it must
check `trashed` on the file object itself, in addition to `removed` on
the Change wrapper — missing either one leaves genuinely-gone content
looking live.

### 1c. No connector-state persistence mechanism exists anywhere — confirmed by search, not assumed

Grepped the whole tree for every plausible name
(`start_page_token`, `cursor`, `connector_state`, `sync_cursor`,
`delta_token`, etc.). Every real hit is one of two *different* concepts:
`search()`/`browse()`'s own opaque `{sync_id, offset}` response-pagination
cursor (client-facing, never stored server-side), or `list_drive_files`'s
own `pageToken`/`nextPageToken` loop (discarded the moment that one
listing call finishes). Neither persists anything across separate sync
*runs*. `sync_state` (migration `0007`) is a single global row
(`current_sync_id` only) built for a different purpose — checking
whether *any* re-sync has happened since a client's cursor was issued —
and is the wrong shape for a per-connector value entirely. The only
cross-run connector value that exists today is the OAuth token, and it's
file-based (`.google_drive_token.json`), not in the schema at all.

### 1d. `sync_drive`'s current (phase 4) shape

Full listing every run, an in-memory `source_modified_at` fast-skip for
non-shortcuts, a full unscoped tombstone sweep comparing every
previously-seen `source_id` against this run's listing. This entire
approach is what Changes API replaces when a valid cursor is available —
not a separate parallel path, an upgrade to the same function.

---

## 2. Spec-locked — not open for reinterpretation

| # | Locked decision | Source |
|---|---|---|
| 1 | The Changes API is *the* delta primitive — it alone surfaces tombstones. `modifiedTime` stays a secondary fast-skip only; SHA-256 still decides skip vs. repoint. | §8.1 |
| 2 | Scope stays My Drive + Shared-with-me only — Shared Drives excluded. | §8.1 (already locked in phase 1/4) |
| 3 | A partial/failed pull never tombstones. | §5.3 |
| 4 | Deleted-source lifecycle: tombstone the provenance link, keep `documents`/`chunks` for lineage. | §5.2 |

---

## 3. Genuinely open — decisions needed, proposed defaults in italics

### 3.1 Persistence: a new generic `connector_state` table, not a Drive-specific one, not the OAuth token's file pattern

*Default:* a new migration adds:

```sql
CREATE TABLE IF NOT EXISTS connector_state (
    connector   TEXT NOT NULL,
    key         TEXT NOT NULL,
    value       TEXT,
    updated_at  TEXT NOT NULL,
    PRIMARY KEY (connector, key)
);
```

Drive stores `(connector='drive', key='start_page_token', value=<token>)`.
Rationale: a Drive-specific table or column would work for Drive alone,
but this is exactly the kind of thing the project's stated review preference
already names directly — *"a module expected to grow should get its own
[structure] from day one"* — and a connector needing to remember some
small scalar between runs is not a Drive-only concern; Notion (J-12,
deprioritized but still on the roadmap) may need the same shape for
whatever cursor concept it has, if any. A generic key-value table serves
any future connector's persisted-state need without a new migration each
time. `sync_state` (§1c) is deliberately *not* reused or reshaped — it's
a single global row for a different, already-load-bearing purpose,
`0007`'s own header explains why it's shaped that way, and repurposing it
would be exactly the kind of "edit something already working" move this
project's own hardest-learned rule warns against.

**The OAuth token deliberately stays file-based, not moved into this
table.** It's a secret, not a cursor — different concern, different
handling expectations (already gitignored, already working, already
tested end-to-end). Flagged here as a considered-and-rejected symmetry,
not an oversight.

### 3.2 `sync_drive` becomes hybrid, in place — not a second function

*Default:* `sync_drive`'s signature and return shape are unchanged from
phase 4. Internally: read the stored `start_page_token` for this
connector. If present, run the incremental path (§3.3). If absent (true
first run, or the fallback case in §3.4), run phase 4's existing full
listing + fast-skip + sweep path unchanged, then — **only when the run
had zero errors** — call `changes.getStartPageToken()` and persist it at
the end. `pipeline.run_drive_ingest` needs zero changes — it already just
calls `sync_drive` and treats its stats dict opaquely.

**Cursor persistence is gated on `errors == 0`, exactly like the sweep.**
A full run that could not ingest every file must not advance (or, on this
path, first mint) the cursor: leaving no stored token means the next run
replays the full path and retries the file that failed, instead of an
incremental run silently skipping it because Changes API only reports
what changed *after* the token. Same principle, same condition, as
phase 4's rule that an errored pull never runs the tombstone sweep.

*Honest note: this gate was not in the original draft of this section —
it was caught during implementation (the first cut persisted the cursor
unconditionally at the end of the full path) and is recorded here after
the fact, not decided at design time. Matches the sweep gate that was
already there.*

*Second honest note (adversarial review): the cursor-mint step
(`getStartPageToken` + `set_connector_state`) is now wrapped in its own
`try/except`. Before, a blip on that one call — after every file was
already fetched, hashed, committed and the sweep already run — raised out
of `sync_drive` → `run_drive_ingest`, so chunk/embed/organize were
skipped and a fully successful pull reported as a total failure. Now a
mint failure logs a traceback and returns the run's real stats anyway; a
missing cursor just means the next run does a full sync, which is exactly
the §3.4 fallback posture.*

### 3.3 The incremental path needs neither the fast-skip nor the sweep

*Default:* for each `Change` entry: if `removed` is true, or the
embedded `file.trashed` is true, tombstone that `fileId` directly
(`db.tombstone_document_source`) — no listing comparison needed, Changes
API already told us definitively what's gone. Otherwise, call the
existing `process_drive_file` on the embedded `file` object exactly as
the full-listing path already does (same shortcut resolution, same
staging, same hand-off — nothing new needed there). The `source_modified_at`
fast-skip (phase 4 §3.2) doesn't apply here: every entry Changes API
returns is by definition something that changed, there's nothing to
skip. No `_common.sweep_vanished_sources` call either — there is no
"compare everyone seen against everyone previously seen" step in this
path at all, removals arrive as their own explicit entries.

**The new cursor (`newStartPageToken`) is persisted only when the run
had zero errors** (same gate as §3.2). If any change's `process_drive_file`
raised, the previously-stored token is left untouched, so the next run
replays this exact same batch from the same starting point. That is safe:
`process_drive_file` is idempotent (content-hash dedup all the way down),
so the changes that already succeeded are just harmlessly reprocessed
alongside a real retry of the one that failed.

`changes.list`'s own `fields` param must mirror `list_drive_files`'s
scoping intent: request `fileId, removed, file(id, name, mimeType,
modifiedTime, shortcutDetails, driveId, trashed)`, and — matching
`list_drive_files`'s existing, deliberate omission — do **not** set
`includeItemsFromAllDrives` / `supportsAllDrives`. Leaving these unset on
`files.list` but setting them on `changes.list` would let Shared Drive
changes leak into the incremental path even though the full-listing path
correctly excludes them, silently reopening the very scope boundary §8.1
locks.

### 3.4 A rejected/invalid cursor falls back to a full sync, broadly

*Default:* wrap the `changes.list` call in a broad `try/except HttpError`
— broader than the narrow, specific-status-code matching used elsewhere
(e.g. `resolve_shortcut_target`'s 403/404 handling). This is a
deliberate difference, not an inconsistency: for a broken shortcut,
there is no safe fallback, so narrow matching (only handle what's
specifically understood, re-raise everything else) is correct. Here, the
fallback (a full sync) is *always* a safe, correct degradation regardless
of *why* the cursor was rejected. If the underlying problem is actually
something else (auth, quota, a genuine outage), the full-sync fallback
will hit the same failure and surface it through the normal error path
already handled per-file — nothing gets silently masked, because a
failing fallback fails loudly the same way phase 4 already does. On any
error from `changes.list`: log the traceback naming it a
cursor-invalidation fallback, discard the stored token, and run the full
path as if none existed.

*Honest note: the first cut of this caught only `HttpError`. After the
adversarial review, that was widened to `except Exception` — because once
retry/backoff (phase 3) is in front of the call, `googleapiclient`
re-raises transport-level failures (`ssl.SSLError`, `socket.timeout`,
`ConnectionError`, `httplib2.ServerNotFoundError`) **directly, not
wrapped in `HttpError`**, so those were escaping this fallback entirely
and contradicting this section's own "regardless of why" wording. The
log call was also bumped from `logger.warning` to `logger.exception` so
the traceback of an unexpected type is kept even though the run recovers.
Widened after the fact, not decided at design time.*

---

## 4. Out of scope for this phase

- Retry/backoff on any Drive API call (phase 3, unchanged).
- The `changes.watch()` push-notification/webhook mechanism — spec never
  asks for real-time push, only a better re-sync; polling via
  `changes.list` on manual trigger (§5.2's "connector-level" re-sync)
  is what's specified.
- Any change to `sync_state` or the response-pagination cursor mechanism
  (§1c) — unrelated, unaffected, deliberately left alone.

---

## 5. Testing approach

Extends the existing `test_drive_connector.py` stub pattern; `_FakeFiles`
gains a sibling for `.changes()` (`.getStartPageToken()`, `.list()`).

- No stored token → full-listing path runs (phase 4's existing tests
  already cover this shape); a fresh token is persisted at the end via
  `changes.getStartPageToken()`.
- A stored token → incremental path runs; `list_drive_files` is **not**
  called (assert the fake would raise if hit, mirroring how phase 4
  proved the fast-skip path never touches `fetch_file_bytes`).
- A `removed: true` change tombstones that `fileId` directly, no sweep
  call made.
- A change whose `file.trashed == true` also tombstones, exercised as a
  distinct case from `removed` so the two signals can't silently collapse
  into "only one was actually implemented."
- A change with neither flag set processes normally through
  `process_drive_file` (including a shortcut change, to confirm the
  incremental path reuses shortcut resolution correctly).
- Multi-page `changes.list` results are all consumed before the new
  token (from the final page's `newStartPageToken`) is persisted.
- An `HttpError` from `changes.list` triggers the full-listing fallback
  and a fresh token gets minted — assert both the fallback ran and the
  old (invalid) token was discarded, not silently reused next time.
- `changes.list`'s request never sets `includeItemsFromAllDrives`/
  `supportsAllDrives`, mirroring the existing `files.list` scope test.
- A full run with one file erroring does **not** mint/persist a cursor —
  `get_connector_state` still returns `None` afterward, and a second run
  takes the full path again, not the incremental one (§3.2 gate).
- An incremental run where one change's `process_drive_file` raises does
  **not** advance the stored token — after the run it is still the
  original token, not that run's `newStartPageToken` (§3.3 gate).

---

## 6. Implementation shape

New migration, additive only:

```sql
-- 0010_connector_state.sql
CREATE TABLE IF NOT EXISTS connector_state (
    connector   TEXT NOT NULL,
    key         TEXT NOT NULL,
    value       TEXT,
    updated_at  TEXT NOT NULL,
    PRIMARY KEY (connector, key)
);
```

New `db.py` functions (additive only):

```python
def get_connector_state(conn, connector: str, key: str) -> Optional[str]:
    """None if never set - the correct signal for 'no stored cursor yet,
    run the full path.'"""

def set_connector_state(conn, connector: str, key: str, value: str, updated_at: str) -> None:
    """INSERT ... ON CONFLICT(connector, key) DO UPDATE - same upsert
    pattern already used throughout db.py."""
```

`drive.py`: `sync_drive`'s body gains a branch at the top (stored token
present → `_sync_drive_incremental`; absent → the existing phase 4 body,
renamed internally to `_sync_drive_full`, both called from the same
public `sync_drive` so the signature and pipeline wiring never change).

---

## 7. Summary

- No existing mechanism conflicts with or could be repurposed for this —
  confirmed by a real search, not assumed from memory of what "probably"
  exists.
- One new, genuinely generic table (`connector_state`), deliberately not
  Drive-specific and deliberately not reusing `sync_state` or the OAuth
  token's file pattern — each existing mechanism serves a different,
  already-load-bearing purpose.
- `sync_drive` gains a mode internally; nothing above it (`pipeline.
  run_drive_ingest`) needs to change at all.
- The incremental path is strictly simpler than the full path it
  replaces when available — no fast-skip, no sweep, both rendered
  unnecessary by Changes API telling you directly what changed and what
  vanished.
- A rejected cursor degrades safely and loudly to a full sync, verified
  as the documented, correct behavior rather than assumed.
