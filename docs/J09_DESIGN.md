# J-09 — Reconciliation engine: design

**Status:** matching direction **confirmed** (§0). The maintainers
chose to proceed with the best-supported approach and review it afterwards.
Implementation is under way against this doc.

**Ticket:** GitHub Issue #9 (J-09 — Reconciliation logic). Consumes J-08's
`OrganizeResult` (PR #43). Spec source of truth: `mvp-spec.md` §6, plus §4
(event log), §5.3 (Phase B / atomic swap / no-op skip / partial-pull),
§7.3 (stale ids).

---

## 0. Matching algorithm — CONFIRMED

**Direction:** many-to-many **threshold** matching — build the
full new×old similarity matrix (centroid-cosine + membership-Jaccard
blend), keep every pair scoring above τ, and derive each structural event
directly from the **cardinality** of that match graph. **Not** Hungarian
1:1 assignment.

**Why this is a real decision and not just "pick one".** `mvp-spec.md` §6
contradicts itself here:

> "match new clusters to existing Topics by a weighted blend of
> centroid-cosine + membership-Jaccard (**Hungarian assignment** above
> threshold τ)"

…but the very next paragraph:

> "Structural-mutation vocabulary = { born, tombstone, rename, reparent,
> split, merge }, every one **derived** from new↔old mapping cardinality
> — none commanded:
> - 1 old : many new → **split** · many old : 1 new → **merge**"

Hungarian assignment is a **strict 1:1 bipartite matching** — it returns
at most one partner per node on each side, by construction. It can
therefore *never* produce a `1 : many` or `many : 1` mapping, so `split`
and `merge` cannot be "derived from cardinality" under it. You'd have to
bolt on a *separate* split/merge detector after the assignment — which
directly contradicts "every one derived … none commanded". A many-to-many
threshold match produces `split`/`merge` as a natural reading of the match
graph's shape, with no second pass.

**Cost of the change:** it's a documented deviation from a spec line. The
mechanism the spec actually cares about (events *derived* from cardinality,
never commanded — the thing that makes organization *evolve*) is preserved
and arguably only works this way. Everything else in §6 is untouched.

**External support:** many-to-many overlap matching for community
tracking across snapshots — with merge/split defined by match cardinality,
not 1:1 assignment — is the established approach in the dynamic
community-detection literature (Greene, Doyle & Cunningham 2010 and
independent follow-on work). This is the standard technique for exactly
this problem, not a bespoke choice.

**Deviation recorded:** spec §6's "Hungarian assignment" phrase is
superseded by many-to-many threshold matching, because §6's own
"every mutation derived from cardinality (incl. split/merge)" requirement
is incompatible with strict 1:1 assignment. Flagged here for maintainer
review.

---

## 1. Spec-locked — not open for reinterpretation in J-09

These come straight from the spec; J-09 implements them as written.

| # | Locked decision | Source |
|---|---|---|
| 1 | **Full re-cluster every re-sync, then match new→old.** Not incremental placement ("incremental placement ossifies structure"). J-08 already does the full re-cluster; J-09 does the match. | §6 |
| 2 | **Match signal = weighted blend of centroid-cosine similarity + membership-Jaccard** (chunk-id sets; chunk ids are stable content hashes → reliable cross-sync signal). | §6, §4 |
| 3 | **A threshold τ gates a match.** Below τ = not the same node. | §6 |
| 4 | **Mutation vocabulary is exactly `{ born, tombstone, rename, reparent, split, merge }`** — no others, and every one **derived** from mapping cardinality, none explicitly commanded. | §6 |
| 5 | **Cardinality → event mapping:** 1:1 above τ → same node (→ `rename` if label changed, `reparent` if parent changed); 1 old : N new → `split`; N old : 1 new → `merge`; old matches nothing → `tombstone`; new matches nothing → `born`. | §6 |
| 6 | **`tombstone` is never a hard delete.** Row kept, `tombstoned = 1`, best-effort `superseded_by` lineage. Stale topic-id lookups must still resolve (§7.3). | §6, §7.3 |
| 7 | **Re-label discipline:** only re-label a node that *materially changed* — born / split / merged, OR member-set Jaccard vs. its own prior self dropped below a threshold, OR centroid drifted past a threshold. Low temperature; pass the prior label as a "keep if still accurate" hint. J-08 already exposes the `prior_label` hook on `label_cluster`. | §6 |
| 8 | **Versioning = the append-only `topic_events` log**, keyed by `(topic_id, sync_id)`. Event types constrained by the existing `CHECK` to the six above. Not full snapshots. `db.insert_topic_event` / `get_events_for_topic` already exist. | §4, migration 0001 |
| 9 | **Topic identity = UUID + centroid + member-set, owned in our layer.** Never something clustering returns — J-08 emits transient `ClusterNode.key`s; J-09 assigns/carries the UUIDs. | §4, §6 |
| 10 | **Phase B is atomic.** Build the new hierarchy off to the side, swap in atomically, never expose a partial hierarchy to a query. `graph_db/staged_build.build_and_swap()` is the mechanism (validates completeness → atomic file swap → SQLite cache sync). | §5.3 |
| 11 | **Organize+reconcile fires only if the live chunk set changed** (≥1 chunk added / retired / content-changed). A true no-op re-sync ⇒ **zero events**. | §5.3 |
| 12 | **A partial / failed pull never tombstones and never reconciles** — prior hierarchy and corpus left fully intact; just re-run. | §5.3 |
| 13 | **`Unfiled` is never matched, renamed, reparented, or tombstoned.** Fixed reserved identity (J-08 owns the row). Chunks entering/leaving it are placement, not topic mutations. | §6 |
| 14 | **Constants** (blend weight, τ, "materially changed" thresholds) are **prototype-tuned against real data, not guessed** — config constants, tuned once the real corpus + a second re-sync exist. | §6, §9 |
| 15 | **No global quality objective function. No optimizer.** Improvement is emergent from re-clustering a changed corpus + reconciliation. | §6 |
| 16 | **Depth cap ~4–5 and minimum branch fan-out** must still hold after any `reparent`. J-08 enforces them on build; J-09 must not violate them when carrying UUIDs onto a re-clustered tree of a different shape. | §6, CLAUDE.md |

---

## 2. Genuinely open — developer calls, need a decision (proposed defaults in italics)

Mirrors J-08's "three open questions" format. Each of these is left to the
developer by the spec; I'll build the italicised default unless redirected.

1. **Greedy match-graph construction.** Build the new×old score matrix,
   threshold at τ, then read connected components: a component with one
   old + one new = 1:1; one old + many new = split; many old + one new =
   merge; many:many = *?*. *Default: treat a many:many component as the
   dominant old node `merge`-ing the rest, then `split`-ing — but log it
   loudly and cap component size; a true many:many blob usually means τ
   is too low. Tie-break by higher blended score, then by larger Jaccard.*

2. **Blend weight `w`** in `score = w·cosine + (1−w)·jaccard`. Spec says
   "weighted", gives no number. *Default: `w = 0.6` (centroid slightly
   favoured — it's robust to a few chunks moving; Jaccard is the
   tie-breaker and the split/merge signal). Config constant
   `RECONCILE_CENTROID_WEIGHT`. Tuned against the real re-sync later.*

3. **τ: one global value or per-level?** Placement threshold is per-level
   (§6); τ is written singular. *Default: one global `RECONCILE_MATCH_TAU
   = 0.5` to start, revisit per-level only if real data shows deep nodes
   systematically failing to match.*

4. **"Materially changed" thresholds** for the re-label gate (locked
   decision #7 names them, sets neither). *Defaults:
   `RELABEL_JACCARD_DROP = 0.5` (self-vs-prior-self membership Jaccard
   below this ⇒ re-label), `RELABEL_CENTROID_DRIFT = 0.15` (1 − cosine to
   prior centroid above this ⇒ re-label). Config constants.*

   **Re-label *mechanism* (simplification):** J-09 does not call the LLM.
   J-08 already labels every cluster fresh each run (with the `prior_label`
   hook available). So the re-label gate reduces to a **pick**: a
   carried-forward node that did *not* materially change **keeps its prior
   name/description** (name churn avoided); one that *did* (born / split /
   merged / drift past threshold) **takes J-08's fresh label**. A future
   refinement can re-invoke `label_cluster(prior_label=...)` for the
   changed-but-matched nodes specifically; not needed for MVP and it keeps
   J-09 free of an LLM dependency. Recorded as a deliberate deviation from
   the literal "pass the prior label as a hint to the model" wording.

5. **Which UUID survives a split / merge, and the `detail_json` shape.**
   *Default: on **split**, the new node with the largest membership
   overlap with the old node keeps the old UUID; the others are `born`,
   each carrying `detail_json = {"split_from": "<old_uuid>"}`; the
   surviving node logs `{"split_into": [<new_uuids>]}`. On **merge**, the
   old node with the largest overlap with the new node keeps its UUID and
   logs `{"merged_from": [<old_uuids>]}`; the other old nodes are
   `tombstone`d with `superseded_by` = the survivor and
   `detail_json = {"merged_into": "<survivor_uuid>"}`.*

6. **Carried-forward centroid.** A matched node keeps its UUID — does it
   keep its old centroid or take the freshly re-clustered one? *Default:
   the new centroid always wins (it's a full re-cluster; the old centroid
   is stale by definition). The old centroid is still readable from the
   event log lineage if ever needed.*

7. **`rename` + `reparent` on the same 1:1 match.** *Default: two separate
   events in the same `sync_id` (the vocabulary lists them separately and
   the log is append-only; a consumer folds them by sync if it wants).*

8. **First sync (no existing topics).** *Default: every base/parent node
   is `born`; no matching runs. Trivial, stated for completeness.*

9. **J-08 / J-09 boundary.** *Default (matches J-08 PR #43): J-08 stays
   persistence-free except the `Unfiled` row. J-09 owns every `topics` /
   `parent_of`-equivalent / `contains`-equivalent / `topic_events` write,
   and drives `staged_build.build_and_swap` with a `populate_fn` that
   walks the reconciled tree.*

10. **Reconcile entry point & wiring.** *Default:
   `reconcile.reconcile(sqlite_conn, organize_result, *, sync_id,
   live_graph_path) -> ReconcileResult` (events written, counts,
   low-confidence passthrough from J-08). Called from Phase B in
   `pipeline.py` only after a fully successful pull with a changed live
   chunk set. Not wired into `pipeline.py` in this ticket unless asked —
   J-08 isn't either.*

---

## 3. Out of scope (seams preserved, do not build — spec §6 MVP boundary)

- Conversation / feedback-driven hierarchy evolution (Part 2b).
- User editing of the hierarchy (machine-owned in MVP).
- The full knowledge graph; any multi-parent / DAG topics.
- Any global quality score or optimizer.

---

## 4. Testing approach (once confirmed)

Synthetic before/after cluster sets with hand-checkable cardinality, no
real API calls (stub embedder/LLM, as in `test_clustering.py`):

- 1:1 stable → same UUID, zero events
- 1:1 with changed label → `rename` only; unchanged label → no `rename`
- 1:1 with changed parent → `reparent`
- 1 old : 2 new → `split`, correct UUID survival, `detail_json` shape
- 2 old : 1 new → `merge`, loser `tombstone`d with `superseded_by`
- old with no match → `tombstone` (row kept, not deleted)
- new with no match → `born`
- **no-op re-sync ⇒ zero events** (spec §5.3)
- partial-pull path ⇒ hierarchy + event log untouched
- re-label gate: Jaccard-drop and centroid-drift each trigger; neither ⇒ label kept
- stale topic-id after `tombstone` resolves via `superseded_by` (§7.3)
- determinism: same inputs ⇒ same events, same UUIDs (protects the log from churn)
- `build_and_swap` completeness validation still passes on a reconciled tree

---

## 5. Implementation shape

Two modules (CLAUDE.md: one job per file):

- **`jarvis/modules/organize/matching.py`** — pure, no I/O. Given new
  nodes (from `OrganizeResult`) and prior nodes (id, centroid, member
  set, name, parent), builds the thresholded new×old match graph, reads
  connected-component cardinality, and returns a list of typed
  `MutationEvent`s + the new→UUID assignment. Fully unit-testable with
  hand-built before/after sets.
- **`jarvis/modules/organize/reconcile.py`** — orchestrator. Loads prior
  topic state from the SQLite cache (`topics` + `chunks.topic_id`),
  calls `matching`, applies the re-label pick (§2.4), writes `topics`
  rows + `topic_events`, and persists the reconciled tree through
  `graph_db.staged_build.build_and_swap` (completeness-validated atomic
  swap + cache sync). Entry point
  `reconcile(sqlite_conn, organize_result, *, sync_id, live_graph_path=None) -> ReconcileResult`.

New DB helpers in `db.py`: `update_topic`, `mark_topic_tombstoned`,
`get_live_topics` (there is currently only `insert_topic` / `get_topic`).

Constants land in `organize/config.py` (`RECONCILE_MATCH_TAU`,
`RECONCILE_CENTROID_WEIGHT`, `RELABEL_JACCARD_DROP`,
`RELABEL_CENTROID_DRIFT`) — placeholders to tune against the real corpus +
a real second re-sync, per spec §6/§9.

**Pipeline wiring (follow-up, now done):** `run_filesystem_ingest` runs
organize + reconcile as spec §5.3 Phase B — after a successful pull, and
only when the live chunk set actually changed (a no-op re-sync skips it,
producing zero new `topic_events`). Surfaced one J-08 seam fix:
`organize(restrict_to_chunk_ids=...)` so a deleted source's still-cached
embedding is excluded from the re-cluster (spec §5.1/§5.2). Chunks whose
sources are all tombstoned fall to Unfiled via reconcile's safety net and
are already filtered out of `search()` by J-07's unconditional
`tombstoned = 0` join — so they are out of the searchable corpus in
effect. Flagged for review: a cleaner fix is for `staged_build` validation
to exclude fully-tombstoned chunks rather than requiring them placed.

## 6. Summary

- Many-to-many threshold matching, confirmed (§0); Hungarian phrasing in §6 superseded, flagged.
- 16 spec-locked decisions (§1), implemented as written.
- 10 developer-judgment calls (§2), each with a default; re-label mechanism simplified so J-09 needs no LLM.
- Constants are tunable placeholders, not frozen guesses.
