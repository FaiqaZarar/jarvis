# J-11 — Drive connector: shortcut resolution + shared-drive identity: design

**Status:** findings + proposed defaults, no implementation yet. Same
discipline as J-09/J-10. This covers the gap identified in PR #68 review:
`mvp-spec.md` §8.1's Identity bullet ("resolve shortcuts to their target,
set shared-drive flags on the provenance row") is not yet implemented,
and PR #68's docstring did not disclose the gap until review caught it.

**Ticket:** J-11 phase 1 (PR #68), Identity sub-scope. Spec source of
truth: `mvp-spec.md` §8.1.

---

## 1. Verified current state

### 1a. What PR #68 already does

`drive.py` correctly implements `source_id = fileId`, exports Docs to
Markdown, downloads native `.docx`/PDF, stores the *original* Drive
mimeType (not the exported format) on `document_sources.mime`, and hands
off to the shared `_common.ingest_staged_file` for the
normalize/hash/commit/retry-safe sequence. Confirmed via the review
bundle and a full `pytest tests/test_drive_connector.py -v` pass (22
tests, all green as of the `_common.py` extraction review — separate
from this doc's scope).

### 1b. What's missing, confirmed by review, not by assumption

Neither shortcut resolution nor a shared-drive flag exists anywhere in
`drive.py`. A Drive shortcut (`mimeType:
application/vnd.google-apps.shortcut`) falls through `fetch_file_bytes`'s
generic "unsupported type" branch today — logged at INFO, silently
skipped, indistinguishable from a Sheet or a Slide. Content a user placed
via a shortcut (a very common Drive sharing pattern) currently never gets
ingested, with no error and no visible signal that anything was missed.

### 1c. `shortcutDetails.targetMimeType` is a stale snapshot — verified against Drive's own docs

Google's API reference states plainly: *"The value of this field is a
snapshot of the target's MIME type, captured when the shortcut is
created."* Not live. A design that branches on this field directly (Doc
vs `.docx` vs PDF vs unsupported) without a fresh `files.get` call would
work today and silently misroute or fail later if the target file is ever
converted or replaced — exactly the kind of thing that looks fine in
review and breaks in production. This rules out the naive approach.

### 1d. `document_sources` schema — real, verified via migrations 0001–0008

`native_metadata_json TEXT` has existed since `0001`, documented there as
*"raw source-native metadata, JSON blob,"* and is currently unwritten by
any connector. Every other Drive/filesystem-specific column added since
(`source_mtime`, `source_size`, `source_inode` in `0002`; `title` in
`0008`) used a plain additive `ALTER TABLE ... ADD COLUMN` — only `0004`
needed a full table rebuild, and only because it had to *drop* a CHECK
constraint, which SQLite can't do via a simple `ALTER`. A new column for
a shared-drive flag would follow the `0002`/`0008` precedent exactly, no
rebuild needed either way.

---

## 2. Spec-locked — not open for reinterpretation

| # | Locked decision | Source |
|---|---|---|
| 1 | `source_id` = Drive `fileId`. For a shortcut, this is the **shortcut's own** fileId, not the target's — see §3.1, this is a judgment call built on top of the locked identity rule, not a deviation from it. | §8.1 |
| 2 | `document_sources.mime` describes the *source*, never the transfer/export format. | §8.1 (already correctly implemented for the non-shortcut path) |
| 3 | Shared Drives are out of MVP scope; My Drive + Shared-with-me only. | §8.1 |
| 4 | Multi-parent is moot under content-addressing — a shortcut is not a multi-parent problem, it's an identity-resolution problem (a different `source_id` pointing at content that may already exist under another `source_id`, which content-hash dedup already handles for free once the bytes are fetched correctly). | §8.1 |

---

## 3. Genuinely open — decisions needed, proposed defaults in italics

### 3.1 Identity: does `source_id` follow the shortcut or the target?

Two readings are defensible. *Default: the shortcut.* `source_id` stays
the shortcut's own `fileId`. Rationale: the shortcut is the thing the
user actually placed and can remove independently of the target's own
lifecycle (deleting a shortcut must tombstone *that* provenance row
without touching the target if it's also ingested directly or via
another shortcut elsewhere). This mirrors how the filesystem connector
already treats identity as "the place," not "the bytes" — `source_id` =
path, content = hash, and the exact same separation applies here:
`source_id` = the shortcut's place in the user's Drive, content = the
resolved target's bytes, unified by the same content-hash dedup that
already merges identical content from any source.

*Alternative:* `source_id` = the target's `fileId`. Rejected — two
shortcuts to the same file would collapse to one `document_sources` row,
silently dropping one of the user's two actual Drive locations for that
content, which the filesystem-connector precedent above says should be
two provenance rows pointing at one deduped document, not one.

### 3.2 Resolution mechanism: live `files.get`, never the cached snapshot

*Default:* on encountering `mimeType == shortcut`, call
`files.get(fileId=<shortcutDetails.targetId>, fields="id, name,
mimeType, modifiedTime")` for fresh target metadata. Cost: one extra API
call per shortcut encountered. This is the only option consistent with
§1c's finding — using the snapshot risks routing a converted/replaced
target through the wrong export path months after the shortcut was made,
which fails exactly when nobody's looking at this code anymore.

### 3.3 What identifies the fetch, vs what identifies the row

*Default:* `export_media`/`get_media` calls use the **resolved target's**
`fileId` and real mimeType (this is what actually determines Doc-export
vs `.docx`/PDF-download vs unsupported). `document_sources.mime` is set
to the **target's real resolved mimeType**, not the shortcut's own
`application/vnd.google-apps.shortcut` type — storing the shortcut's own
mime would make `search()`'s `file_type` filter useless for any content
reached via a shortcut, defeating the point of resolving it at all.
`document_sources.title` comes from the **target's** name (more useful
for search than a shortcut's often-generic "Shortcut to X" display name;
flagged here as a real choice, not an oversight). `source_id` and
`source_url` stay on the **shortcut's own** `fileId` per §3.1.

`document_sources.source_modified_at` also follows the **resolved
target**, for the same reason as `mime`/`title`: it records when the
*content* was last changed (`search()`'s `date_range` filter matches
against it, per `models.py`'s `DateRange` docstring), and a shortcut's
own `modifiedTime` is placement metadata, not content recency. Honest
note: this one was not called out in the original draft of this section
— it surfaced during implementation as a doc gap and is recorded here
after the fact, not decided at design time.

### 3.4 Failure modes — each gets a specific, honest log, never a silent drop or a crash

| Condition | Detection | Behavior |
|---|---|---|
| Broken shortcut (no `targetId` in `shortcutDetails`) | `shortcutDetails.get("targetId")` is `None`/missing | WARNING, `"skipped"`, same as any other skip |
| Target vanished or inaccessible | `files.get` raises `HttpError` with a 404 | WARNING naming the shortcut, `"skipped"` |
| Target requires a resource key we don't pass | `files.get` raises `HttpError` (403/404) **and** `shortcutDetails.targetResourceKey` is present | WARNING distinguishing this from a plain vanished-target 404 — *"target requires a resource key, not yet supported"* — so this specific, real, known Drive gotcha doesn't get misread as "file was deleted" during any future debugging |
| Target is itself a shortcut (chaining) | resolved target's own `mimeType == shortcut` | WARNING, not recursed, `"skipped"` — single-hop resolution only |
| Any other `HttpError` | anything else | re-raise — a real transport error, not a skip, matching `fetch_file_bytes`'s existing non-size-error behavior |

*Alternative considered for the resource-key case:* implement full
resource-key plumbing (threading `X-Goog-Drive-Resource-Keys` through
`files.get`/`export_media`/`get_media`). Rejected for this phase —
real added complexity across multiple call sites, for a narrower edge
case within an already-narrow edge case (a shortcut, in Shared-with-me,
to specifically a resource-key-gated file). Tracked as an honest
deferral in the module docstring, same pattern as the Changes API and
retry/backoff deferrals already there — not silently dropped.

### 3.5 Shared-drive flag: `native_metadata_json`, not a new column

*Default:* store it as a key inside the existing
`document_sources.native_metadata_json` JSON blob (e.g. `{"shared_drive":
true}` when `file_meta.get("driveId")` is present), not a new dedicated
column. Rationale: the column exists since `0001` specifically for
"raw source-native metadata" that doesn't need to be queried
structurally; nothing in spec's `search()` filter set (`topic`, `source`,
`date_range`, `file_type`) references this as a filterable dimension, so
promoting it to its own indexed column would be schema surface with no
current consumer. If a real need to filter/query on this ever appears,
promoting it later is cheap and precedented (`title`, `source_mtime`,
`source_size`, `source_inode` were each promoted from "would-be JSON" to
real columns exactly when they became load-bearing, via a plain additive
migration).

*Alternative:* a dedicated `is_shared_drive_item INTEGER` column via a
new `0010_add_shared_drive_flag.sql`, following the `0002`/`0008`
precedent exactly. Reasonable, and cheap if this reasoning turns out
wrong — flagged so the call is visible, not buried.

---

## 4. Out of scope for this pass (unchanged from PR #68's existing deferrals)

- Changes API delta sync + tombstoning (phase 2).
- Retry/backoff (phase 3).
- `run_drive_ingest()` pipeline entrypoint (phase 4).
- Full resource-key plumbing (§3.4) — detected and logged honestly, not
  implemented.
- Bounded multi-hop shortcut chains (§3.4) — single-hop only.

---

## 5. Testing approach

Extends the existing `test_drive_connector.py` stub pattern (no real API
calls; `_FakeDriveService`/`_FakeFiles` gain a `.get(fileId=...)` method
alongside the existing `.list`/`.export_media`/`.get_media`).

- A shortcut to a Google Doc resolves via `files.get`, stores with the
  **shortcut's own** id as `source_id`, the **target's** real content,
  mime, and name.
- A shortcut to an unsupported type (e.g. a Sheet) skips cleanly, same
  as a direct Sheet would.
- A broken shortcut (missing `targetId`) skips with a WARNING, no crash.
- A shortcut whose target 404s skips with a WARNING, no crash.
- A shortcut whose target 404s **and** carries a `targetResourceKey`
  gets the distinct "resource key not supported" WARNING, not the
  generic vanished-target one.
- A shortcut to a shortcut skips with a WARNING, not recursed into.
- Two shortcuts to the same target dedup to one `documents` row via
  content hash, two `document_sources` rows (mirrors the existing
  `test_identical_content_dedups_to_one_document` pattern).

---

## 6. Implementation shape

One new function in `drive.py` (not `_common.py` — this is genuinely
Drive-specific, shortcuts don't exist in the filesystem or, so far as
known, Notion):

```
def resolve_shortcut_target(service, file_meta: dict) -> Optional[dict]:
    """Live-resolve a Drive shortcut to its target's real metadata.
    Never trusts shortcutDetails.targetMimeType (a creation-time
    snapshot, not live — see §1c). Returns a file_meta-shaped dict for
    the TARGET, or None if broken/vanished/resource-key-gated/chained
    (each logged with a distinct, honest reason)."""
```

**Resolution happens exactly once, at the top of `process_drive_file`,
not inside `fetch_file_bytes`.** `process_drive_file` builds a single
"effective" `file_meta`: the original dict unchanged if it isn't a
shortcut, or `resolve_shortcut_target`'s result if it is. That one
resolved dict feeds *both* `fetch_file_bytes` (for the actual bytes) and
the `title`/`mime` fields passed to `_common.ingest_staged_file` — never
two separate calls to `resolve_shortcut_target` for the same file. Two
calls would be redundant API usage and opens a real, if narrow, race:
the target could vanish between them, giving the fetch and the metadata
inconsistent answers. Under this fix, `fetch_file_bytes` itself needs
**zero changes** — it never has to know shortcuts exist; all the new
complexity lives in one place, before `fetch_file_bytes` is even called.
`source_id`/`source_url` still read from the *original*, unresolved
`file_meta`, per §3.1.

Shared-drive flag write is a small addition to `process_drive_file`'s
existing `_common.ingest_staged_file` call — needs verifying (not yet
confirmed) whether `db.insert_document_source` already exposes a
`native_metadata_json` parameter, since nothing writes to that column
today. If it doesn't, that's a small additive change to `db.py` alongside
this work, not a schema change.

---

## 7. Summary

- Shortcut resolution is real, scoped, connector-specific work — not
  extracted to `_common.py`.
- The one mechanism decision that actually matters: resolve live via
  `files.get`, never trust the cached `targetMimeType` snapshot (§3.2),
  verified against Drive's own API docs, not assumed.
- Identity stays with the shortcut, content/mime/title come from the
  resolved target (§3.1, §3.3) — mirrors the filesystem connector's
  existing path-vs-hash identity split.
- Five distinct, honest failure modes (§3.4), each logged specifically —
  nothing silently drops content or crashes the sync.
- Shared-drive flag goes into the already-existing `native_metadata_json`
  column (§3.5), no new migration, with the alternative flagged in case
  that call is wrong.
