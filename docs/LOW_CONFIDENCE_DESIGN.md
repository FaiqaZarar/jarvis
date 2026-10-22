# low_confidence on the MCP surface: design

**Status:** confirmed (2026-09-03) as a small follow-up ticket.
Findings + proposed approach below, ready to build once reviewed.

**Ticket:** follow-up from the `jarvis-search` skill work, which found
that `low_confidence` (the small-corpus hierarchy warning) never reaches
any MCP tool. Spec source: `mvp-spec.md` §7.1/§7.2 (neither currently
defines this field - this is a genuine addition, not filling in a gap
spec already anticipated).

---

## 1. Verified current state

### 1a. The full real flow, traced end to end

```
clustering._assess_corpus_confidence(total_chunks)     clustering.py:791
    -> (low_confidence: bool, low_confidence_reason: str|None)
    -> stamped onto OrganizeResult (both return paths)

reconcile.reconcile(organize_result, ...)
    -> ReconcileResult.low_confidence = organize_result.low_confidence
    -> effectively write-only from here down - nothing reads it back

pipeline._run_phase_b(...)
    -> dict["organize"]["low_confidence"] = organize_result.low_confidence
    -> low_confidence_reason never makes it into any dict at all

MCP surface (hierarchy.py / models.py)
    -> nothing. HierarchyResult has exactly one field: topics.
```

### 1b. The real reason this needs persistence, not just a new field

`get_hierarchy()` runs at **query time**, against SQLite state a caller
reads later, possibly in a completely separate process/session.
`organize()`/`reconcile()` run at **ingestion time**, and `OrganizeResult`/
`ReconcileResult` are ephemeral - computed once, discarded except for
whatever actually gets written to the DB. There is no live connection
between "the last ingestion run's confidence assessment" and a later
`get_hierarchy()` call unless that assessment is persisted somewhere
queryable. This isn't "add a field to a model," it's "add a small piece
of durable state," and needs to be designed as such.

### 1c. Neither existing state table is the right fit - checked, not assumed

`sync_state` (migration `0007`): single global row, `current_sync_id`
only, its own migration header explicitly ties it to cursor-staleness
detection specifically (`search()`/`browse()`'s response-pagination
contract). `connector_state` (migration `0010`): explicitly per-connector
keyed (`connector`, `key`) - built for a connector-scoped cursor
(Drive's `start_page_token`). `low_confidence` is a **whole-corpus**
signal, not per-connector and not related to cursor staleness. Forcing
it into either existing table would mean two different concerns sharing
one row's meaning - exactly what this project's own hardest-learned rule
(never repurpose something that already has a job) warns against.

### 1d. The exact reason text and threshold, verbatim

```python
_MIN_CHUNKS_FOR_RELIABLE_HIERARCHY = 15  # clustering.py:61

if total_chunks == 0 or total_chunks >= _MIN_CHUNKS_FOR_RELIABLE_HIERARCHY:
    return False, None
reason = (
    f"only {total_chunks} embedded chunk(s) - below the ~{_MIN_CHUNKS_FOR_RELIABLE_HIERARCHY} "
    "where UMAP/HDBSCAN reliably separate distinct topics; groupings and labels "
    "in this run may be arbitrary and should be treated as provisional"
)
```

Note `total_chunks == 0` is explicitly *not* flagged (nothing to be
unconfident about with zero chunks) - only the `1..14` band is.

---

## 2. Spec-locked - not applicable

No spec-locked decisions here; §7.1/§7.2 don't currently define this
field at all. Worth a small spec addendum once built (matching the
project's precedent for the LadybugDB adoption addendum), flagged for
a maintainer decision, not made unilaterally here.

---

## 3. Genuinely open - decisions needed, proposed defaults in italics

### 3.1 Persistence: a new, dedicated single-row table

*Default:* a new migration, mirroring `sync_state`'s exact shape and
pattern:

```sql
CREATE TABLE IF NOT EXISTS organize_state (
    id                     INTEGER PRIMARY KEY CHECK (id = 1),
    low_confidence         INTEGER NOT NULL DEFAULT 0,
    low_confidence_reason  TEXT,
    updated_at             TEXT NOT NULL
);
```

Single-row by the same `CHECK (id = 1)` convention `sync_state` already
uses, for the same reason - a caller only ever needs "what is the current
value," never history. `updated_at` included since `db.py`'s established
convention (`set_connector_state`, `insert_document_source`) always
stamps a timestamp alongside a write.

### 3.2 Where the write happens

*Default:* `pipeline._run_phase_b`, right where `organize_result` is
already available and already the source `_run_phase_b` currently reads
from directly (not `reconcile_result` - matching the existing, if
accidental, precedent that `organize_result` is treated as the
authoritative copy). Write unconditionally whenever Phase B actually
runs. On a genuine no-op re-sync (Phase B skipped entirely per spec
§5.3), the table correctly keeps its value from the last real run - that
value is still accurate, since nothing about the corpus changed.

### 3.3 Where the read happens, and the model shape

*Default:* add two fields to `HierarchyResult` (not `TopicNode` - this is
a whole-tree signal, not a per-topic one, and shouldn't vary by which
subtree a caller requested):

```python
class HierarchyResult(BaseModel):
    topics: list[TopicNode]
    low_confidence: bool = False
    low_confidence_reason: Optional[str] = None
```

`get_hierarchy()` reads `db.get_organize_state(conn)` once per call and
populates both fields on every response, regardless of `root`/`depth` -
this doesn't change based on which part of the tree was requested, it's
a fact about the whole corpus.

### 3.4 New db.py functions

```python
def get_organize_state(conn) -> tuple[bool, Optional[str]]:
    """(False, None) if never written - the correct default before the
    first Phase B run has ever happened."""

def set_organize_state(conn, low_confidence: bool, reason: Optional[str], updated_at: str) -> None:
    """Upsert - same ON CONFLICT DO UPDATE pattern already used
    throughout db.py."""
```

---

## 4. Out of scope for this ticket

- `low_confidence_reason` is not currently read by `reconcile.py` or
  `pipeline.py` at all today - this ticket doesn't change that, it only
  adds the persistence + MCP-surface path. The existing WARNING log at
  ingestion time is unaffected.
- No change to `sync_state` or `connector_state` - both stay exactly as
  they are, this is intentionally a new, separate, single-purpose table.
- Not retroactively backfilling `organize_state` for a corpus that was
  already ingested before this ships - the next real Phase B run (the
  next re-sync where the live chunk set changes) populates it naturally.
  Until then, `get_hierarchy()` correctly reports the "never written"
  default (`False, None`), which is a reasonable, honest default (no
  known reason to distrust the hierarchy, since nothing's told it
  otherwise) rather than something to special-case.

---

## 5. Testing approach

Extends the existing `test_hierarchy.py`/`test_pipeline.py` patterns.

- `get_organize_state` returns `(False, None)` when never written.
- `set_organize_state` upserts correctly (write, then overwrite, confirm
  the second write wins).
- A full pipeline run through a small corpus (under 15 chunks) results
  in `get_hierarchy()` returning `low_confidence: True` with the real
  reason text.
- A full pipeline run through 15+ chunks results in `low_confidence:
  False`, `low_confidence_reason: None`.
- A no-op re-sync (Phase B skipped) leaves a previously-set
  `organize_state` value untouched - `get_hierarchy()` still reports the
  prior run's assessment, not a reset default.
- `get_hierarchy()` reports the same `low_confidence` value regardless of
  `root`/`depth` params (it's not scoped to a subtree).

---

## 6. Summary

- The real fix requires persistence, not just a new field - `organize()`/
  `reconcile()`'s results are ephemeral and don't survive to a later
  `get_hierarchy()` call otherwise.
- Neither `sync_state` nor `connector_state` is the right existing home -
  checked directly against their own documented single-purpose scope,
  not assumed to be reusable.
- One new, minimal, single-row table mirroring an already-established
  pattern (`sync_state`'s own shape), two new small `db.py` functions,
  two new fields on `HierarchyResult`, one read call in `get_hierarchy()`,
  one write call in `_run_phase_b`. Small, additive, nothing existing
  changes.
