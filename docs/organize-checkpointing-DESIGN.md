# Organize-engine checkpointing and config-versioning: design

**Status:** design only, per the project lead's direction. Nothing in
this doc is implemented yet.

**Source:** the project lead's direct instructions (this doc), plus
`jarvis/modules/organize/clustering.py` and `reconcile.py` as they exist
today (read in full for this doc, not recalled from memory), issue #54
(UMAP seed pinning), and issue #66 (the original "~1000 chunks per
batch" figure, referenced verbally - not filed anywhere in this repo's
`docs/`, confirmed via grep).

**Confirmed requirement, as given:**

1. `organize()` processes chunks in batches, not one all-or-nothing pass.
2. Topic labels persist as they're generated, not only at the end of a
   full run - so an interruption doesn't lose already-completed work.
3. Config versioning: UMAP/HDBSCAN parameters live in a config-versions
   table. A version changes only when deliberately updated, never
   automatically. On resume, compare the in-progress run's recorded
   config version against the current one:
   - same version -> continue from the last checkpoint
   - different version, run broken midway -> restart from scratch under
     the new version
   - different version, run already completed under the old version ->
     do **not** force a rerun; leave it as-is until the next natural
     re-sync
4. Embedding-model versioning is explicitly **out of scope**, deferred
   to phase 2.

---

## 1. Verified current state (read the real code, not memory)

### 1a. `organize()` is one all-or-nothing pass today

`clustering.organize()` -> `organize_vectors()` does, in one call, with
nothing persisted until the very end except the reserved `Unfiled` row:

1. Load every live+embedded chunk's vector (`load_chunk_inputs`).
2. One global UMAP reduction + HDBSCAN clustering call over the **whole**
   matrix (`_base_cluster`, clustering.py:251-300).
3. For each base cluster, one LLM call to title it (`label_cluster`,
   the loop at clustering.py:724-743).
4. Parent roll-up (`_build_parent_levels`, clustering.py:404-502): up to
   `MAX_TREE_DEPTH=5` rounds, each re-clustering the current level's
   titles and calling `_label_parent` per surviving group.
5. Placement: every chunk descends root-to-leaf against the now-complete
   tree (`_place_chunk`).
6. Prune empty nodes, return one `OrganizeResult`.

Nothing is written to SQLite or the graph during any of this. The
caller, `reconcile.reconcile()`, then does its own single pass: match
against prior Topics, decide born/tombstone/rename/split/merge, write
`topics` + `topic_events` rows, and persist the tree into LadybugDB via
`staged_build.build_and_swap` (validated, atomic swap - already resilient
in its own right: a crash mid-swap leaves the **prior** graph fully
intact, nothing partial is ever visible to a reader). Both `organize()`
and `reconcile()` run inside `pipeline.run_ingest()`'s single call, under
one `sync_id`, which is itself minted before either starts
(`jarvis/modules/pipeline.py:275-278`).

One structural fact worth stating plainly, because it bounds everything
below: **the daemon serializes ingest requests through one
`asyncio.Lock`** (`jarvis/modules/daemon/server.py`'s `_WRITE_LOCK`,
confirmed by reading it). At most one organize/reconcile attempt is ever
actually in flight per daemon instance. This matters for how "is a run
still running vs. abandoned" gets decided in §4.

### 1b. Where the actual failure-prone, resumable work is

Clustering math (UMAP + HDBSCAN) is fast, in-memory, and - critically -
**cannot be run over a subset of the corpus and still produce a coherent
hierarchy**: HDBSCAN's density-based cluster boundaries depend on seeing
the whole point set together. Splitting the *input* into sequential
batches of, say, 1000 chunks and clustering each batch independently
would produce incoherent, batch-siloed micro-clusters with no defined
way to merge them back into one hierarchy. This is a hard constraint of
the algorithm, confirmed by reading `_base_cluster`/`_reduce_umap`/
`_hdbscan_labels`, not a stylistic choice - it directly shapes what
"batch processing" can mean here (see §3).

The genuinely slow, network-dependent, failure-prone part is the **LLM
labeling loop**, and its two call sites behave differently:

- **Base-cluster labeling** (`label_cluster`, one call per base cluster)
  is confirmed **order-independent**: each call's prompt is
  self-contained (a sample of that cluster's own member texts, plus an
  optional `prior_label` hint) and never references another cluster's
  output. This scales with corpus size (more chunks -> more base
  clusters) and is the ideal, safe unit for checkpointing.
- **Parent roll-up labeling** (`_label_parent`, inside
  `_build_parent_levels`) has a real **same-level ordering dependency**:
  each call is handed "sibling parent names already used" -
  accumulated from earlier in the *same* loop - specifically to avoid
  duplicate names at that level (clustering.py:461,483). This isn't
  independently checkpointable at single-node granularity without
  redesigning the dedup mechanism itself. It's also comparatively cheap
  regardless of corpus size: `MAX_TREE_DEPTH=5` caps it to at most a
  handful of LLM calls per level, not one per chunk.

This split is the basis for the batching design in §2: checkpoint base
clusters individually; treat each parent-roll-up level as one atomic
step, not checkpointed at finer grain (flagged as a scope limitation in
§5, not silently assumed).

### 1c. Topic identity isn't assigned yet at labeling time

A `ClusterNode.key` (e.g. `"base-3"`) is a **transient, within-run
handle** - real Topic UUIDs are assigned later, by `reconcile()`'s
matching against prior topics. This means checkpointed labels **cannot**
be written into the `topics` table directly mid-run: that table
represents reconciled, identity-bearing topics, and writing into it
before matching has run would corrupt the reconciliation model (matching
needs the *old* `topics` rows untouched until reconcile is ready to
write the *new* state). Checkpoints need their own staging table,
separate from `topics` - see §2.

### 1d. Issue #54 (UMAP seed pinning) - confirmed still in place, cross-process gap now closed

Confirmed: `config.RANDOM_SEED` is pinned into UMAP's `random_state`
(clustering.py:217), **and** the actual #54 fix - forcing `init="random"`
below `_SPECTRAL_INIT_MIN_NEIGHBORS=4` - is still present
(clustering.py:192-220), with its reasoning intact in the comment:
spectral init's ARPACK eigensolver consumes NumPy's *global* RNG in a
way `random_state` doesn't control, so pinning the seed alone doesn't
make spectral init reproducible on a small k-NN graph.

Four tests currently guard this
(`test_clustering_is_deterministic_across_runs`,
`test_organize_membership_is_deterministic`,
`test_reduce_umap_is_deterministic_at_small_n_neighbors`,
`test_small_corpus_base_clustering_is_deterministic`) - all four pass
today. **But every one of them calls `organize()`/`_base_cluster()`/
`_reduce_umap()` repeatedly *within a single Python process, in a loop*.
None test determinism across a fresh process/interpreter restart.**

This is exactly the scenario a crash-and-resume implies: a crash means
the *next* attempt is necessarily a new process. The checkpoint design
in §2 depends on rerunning clustering fresh on resume and getting
**identical base-cluster membership** back, so a persisted label can be
safely matched to "the same cluster" it was written for.

**Update: investigated directly, then hardened - this gap is now
closed.** A real cross-process investigation (not a repeated in-process
loop) ran `_reduce_umap`/`_hdbscan_labels` and the full `organize()`
pipeline as genuinely separate `python` subprocess invocations, 30 times
total across both the spectral-init and forced-random-init code paths,
under both Python's default (randomized) `PYTHONHASHSEED` and a fixed
one. **Every run produced byte-identical UMAP coordinates and cluster
partitions - no divergence was found anywhere, with or without a fixed
hash seed.** `PYTHONHASHSEED` was empirically ruled out as a factor for
this code path specifically (`_reduce_umap`/`_hdbscan_labels` never call
Python's built-in `hash()` - that risk exists only in a test-only stub
helper, not in production code).

Because the investigation *found no bug to fix*, pinning
`PYTHONHASHSEED=0` for the daemon process (`jarvis/cmd/daemon.py`,
verified working via a real relaunch - see below) is **defense-in-depth
against a class of variability nothing today was shown to depend on**,
not a fix for an observed failure. It costs nothing and removes one
entire axis of doubt for future library upgrades or untested corpus
scales, but the accurate framing is "hardened preemptively," not
"fixed a confirmed bug."

Two mechanics worth recording, since they weren't obvious going in:
`PYTHONHASHSEED` must be set before the interpreter boots - mutating
`os.environ` from already-running code has no effect on that process's
own hash randomization. The natural fix, re-exec in place via
`os.execve`, **segfaults on this Windows/Python 3.13 build** (confirmed
directly). The working alternative - relaunch as a child `subprocess`
with the corrected environment and propagate its exit code - was
verified to actually produce a child whose `hash()` output matches a
process started with `PYTHONHASHSEED=0` from birth, not just that the
env var is present in `os.environ`. `jarvis/cmd/daemon.py`'s entrypoint
now does this (self-contained, so it's correct regardless of what
launches it); `jarvis.modules.daemon.client._spawn_daemon` also sets it
directly for the common auto-start path, so that path never pays the
extra relaunch hop. Re-verified through the real, unmodified daemon
entrypoint 10 times: every run relaunched exactly once (confirmed via a
changed pid and exactly one "starting on" log line - not a loop) and
came up healthy afterward.

Permanent regression coverage:
`tests/test_clustering_cross_process.py` (marked `slow_cross_process`,
skipped by default - real subprocess spawns cost ~30s+ each from a cold
numba/UMAP JIT compile - run explicitly or in CI via `--run-slow`) now
spawns real subprocesses and asserts identical output, so a future
regression (a library upgrade, a change to the clustering internals) is
caught automatically instead of silently reintroducing an unverified
assumption.

One precondition *is* already confirmed solid, independent of all the
above: chunk-vector read order is stable - `get_all_chunk_vectors` uses
`ORDER BY chunk_id` (`jarvis/internal/db/embedding.py:32`) - so the
input matrix's row order doesn't itself introduce nondeterminism.

### 1e. Existing conventions this design should follow

- **`sync_state` / `organize_state`** (migrations 0007, 0011): single-row
  tables (`id` pinned to 1 via `CHECK`), holding only the *current*
  value, no history. Pattern for "one global current thing."
- **`connector_state`** (migration 0010): generic per-scope key/value
  with a composite primary key. Pattern for small scoped scalars.
- **`topic_events`**: append-only log, never updated or deleted, tagged
  with a caller-supplied `sync_id`, a `detail_json` blob for
  event-specific payload, plain ISO-string `created_at`. Pattern for "a
  permanent record of what happened, once, per event."
- **`write_query`/`read_query`** (`jarvis/internal/db/_middleware.py`):
  every write commits by default per call; `commit=False` lets a caller
  batch several writes into one transaction
  (`embed_and_index_chunks` is the existing example of that pattern).
  This is the exact mechanism "persist labels as they're generated"
  needs - no new transaction plumbing required, just calling an
  already-committing insert function inside the labeling loop.
- **`topics.version`** already exists and means something unrelated: a
  per-topic identity/label-lineage counter, bumped by `reconcile()` on a
  materially-changed carried node. The new concept in this doc is named
  **`config_version`** throughout, specifically to avoid colliding with
  that existing, differently-scoped column.
- **`config.snapshot()`** (`jarvis/modules/organize/config.py:144-146`)
  already enumerates exactly the UMAP/HDBSCAN/placement/labeling knobs
  that matter (`TUNABLE_NAMES`) and returns them as a dict. This is the
  natural payload for a config-version row - no need to invent a second
  enumeration of "which constants count."

---

## 2. Proposed schema

Three new tables.

### 2a. `organize_config_versions` - append-only, never updated or deleted

```
id           INTEGER PRIMARY KEY AUTOINCREMENT
params_json  TEXT NOT NULL   -- config.snapshot()'s dict, JSON-serialized
created_at   TEXT NOT NULL
note         TEXT            -- optional human reason, e.g. "lowered
                              -- HDBSCAN_MIN_SAMPLES after real-corpus
                              -- Unfiled-rate check"
```

"The current version" is simply the row with `MAX(id)` - append-only,
chronological, no separate `is_active` flag needed (mirrors
`topic_events`'s "append, never mutate" philosophy rather than
`sync_state`'s "one mutable row" philosophy, because *history* of what
config each past run used is exactly what this table needs to answer
"was this run's config the same as today's").

A row is inserted only by a deliberate action (a script, or a manual
step run alongside an intentional `config.py` edit) - never inferred
automatically by diffing `config.snapshot()` against the last row on
every organize run. See §5's open question on what happens if the two
drift apart anyway.

### 2b. `organize_runs` - one row per organize()+reconcile() attempt

```
id                 TEXT PRIMARY KEY   -- reuse the existing sync_id, not
                                       -- a new id (see below)
config_version_id  INTEGER NOT NULL REFERENCES organize_config_versions(id)
status             TEXT NOT NULL CHECK (status IN ('in_progress','completed'))
                     DEFAULT 'in_progress'
started_at         TEXT NOT NULL
completed_at       TEXT
```

**Reuse `sync_id` as `id`, don't mint a second identifier.**
`pipeline.run_ingest()` already generates a `sync_id` right before
organize/reconcile runs (`jarvis/modules/pipeline.py:275`) and threads it
into `reconcile()` already - it just isn't threaded into `organize()`
yet. One sync_id already uniquely identifies "this rebuild attempt";
inventing a parallel `run_id` would be two names for the same thing.
This does mean `organize()`'s signature needs `sync_id`/`run_id` added as
a parameter it doesn't take today - a small, mechanical change, not a
new concept.

Row lifecycle: inserted with `status='in_progress'` at the very start of
`_run_organize`, before any clustering/labeling work happens; updated to
`status='completed'` (+ `completed_at`) only after `reconcile()`'s full
persistence has succeeded. See §4 for exactly why that boundary, not an
earlier one.

### 2c. `organize_cluster_checkpoints` - per-base-cluster label, keyed by content, not by transient key

```
run_id               TEXT NOT NULL REFERENCES organize_runs(id)
member_fingerprint   TEXT NOT NULL   -- sha256 of the sorted tuple of
                                     -- this base cluster's member
                                     -- chunk_ids
label                TEXT NOT NULL
description          TEXT NOT NULL
created_at           TEXT NOT NULL
PRIMARY KEY (run_id, member_fingerprint)
```

**Keyed by a content fingerprint of cluster membership, not by
`ClusterNode.key` (e.g. `"base-3"`).** HDBSCAN's own label numbering
isn't asserted stable enough to trust as a resume key across a fresh
process (§1d's determinism gap) - if label numbering ever came out
differently across the interrupted run and the resumed one even though
the *clusters themselves* were identical, keying by `"base-3"` would
silently attach an old label to whatever cluster happens to be numbered
3 this time, which could be wrong content. Keying by a hash of the
member set instead means a checkpoint can only ever match a cluster with
**exactly** the same membership - if clustering came out even slightly
different on resume (whether from the determinism gap in §1d, or a
change in the underlying chunk set - see §5), the fingerprint simply
won't be found, and that cluster falls through to a normal fresh LLM
call, exactly like an uninterrupted run. This degrades safely: the worst
case is a redundant LLM call, never a label silently attached to the
wrong content.

**Resume procedure:** rerun the clustering math (fast, deterministic,
no LLM calls) fresh, using the config version recorded on `organize_runs`
for this run - fingerprint each freshly-produced base cluster's
membership, and look it up in `organize_cluster_checkpoints` for this
`run_id`. A hit reuses the persisted label/description and skips the LLM
call; a miss makes a fresh LLM call exactly as an uninterrupted run
would, then checkpoints it before moving on.

**Commit granularity: per cluster, not batched.** Requirement #2 says
labels persist "as they're generated" specifically so an interruption
doesn't lose completed work - the safest reading of that is committing
each cluster's checkpoint row immediately after its LLM call returns,
using `write_query`'s existing default `commit=True` (no batching
needed for durability; SQLite commits are cheap local I/O compared to
the LLM call that produced the row). Batching commits into groups would
only risk losing more than one cluster's work per crash, for a savings
(fewer commits) that doesn't matter at this I/O scale. See §3 for what
the "~1000 chunks" figure governs instead, given this.

**Update, resolving §5's open question:** at real corpus scale (~16k
chunks, ~800+ base clusters observed) a "small, capped-depth roll-up" is
not actually cheap - the first roll-up round alone groups hundreds of
base nodes into dozens of parents, each a `_label_parent` call, and every
crash/restart before reconcile committed threw all of it away. §1b's
same-round sibling-dedup ordering dependency does not in fact require
"redo the whole level" as the checkpoint granularity: `_build_parent_levels`
now checkpoints each parent *group* individually, in the same table and
via the same content-fingerprint mechanism as base clusters (fingerprint
= the group's full underlying chunk-id membership, not the transient
node keys). This is safe because groups within a round are still visited
in the same deterministic clustering order on every attempt, so
`sibling_titles` accumulates identically whether an earlier group's
title came from a fresh call or a checkpoint hit. A grouping that
genuinely differs on resume (re-cluster nondeterminism) just
fingerprint-misses and re-labels fresh - the same degrade path §2c
already accepts for base clusters. See `_build_parent_levels`'s
docstring in `clustering.py` for the full reasoning.

---

## 3. Batch size

The original figure from when #66 was filed: process ~1000 chunks
at a time. Two things needed checking: whether that number is still
reasonable, and - a real finding from reading the current code - whether
the thing it was originally meant to batch is even the right thing to
batch anymore.

**What's confirmed from real code:** clustering cannot be run over
sequential batches of the *input* chunk set and still produce one
coherent hierarchy (§1b) - HDBSCAN needs the whole live+embedded set
together. So "cluster 1000 chunks at a time" in the literal, original
sense doesn't hold up against how the algorithm actually works. This
isn't a preference call; it's a correction based on what the code
requires.

**What the number can still usefully govern:** the *post-clustering*
labeling work, which is where the real batching-and-checkpointing value
is (§1b, §2c). Concretely: label base clusters in a stable order (sorted
by fingerprint, since HDBSCAN's own numbering isn't the resume key
here), and use a running total of ~1000 cumulative member-chunk-count as
a **progress/logging boundary** - "batch N done, M clusters / ~1000
chunks labeled" - and, if a future pass adds cooperative
interrupt/cancellation to `organize()` (not proposed here, out of
scope), a natural place to check for it. Given §2c's per-cluster commit
already handles durability, this repurposes the original number rather than
discarding it - same unit of meaning (a batch is "about 1000 chunks'
worth of work"), applied to the phase that can actually be batched.

**Is 1000 still reasonable at today's real scale?** Checked: no
documented real-corpus timing/scale benchmark exists anywhere in this
repo (`docs/`, `config.py`'s own comments, `J09_DESIGN.md`) - every
organize constant in `config.py` is explicitly "prototype-tuned, not
guessed... expected to be adjusted after seeing the engine run against
the real corpus." I can't independently verify actual clustering or
labeling wall-clock time at 1000+ chunks from anything in this codebase;
that needs a real measurement, not an assertion. What I can say from
what's here: `_MIN_CHUNKS_FOR_RELIABLE_HIERARCHY=15` and multiple
comments describing "the MVP corpus is small" suggest real corpora
exercised so far are far below 1000 chunks - meaning, at today's actual
scale, essentially every run will be exactly one batch regardless of the
threshold chosen. 1000 is therefore neither validated nor invalidated by
real data yet; it's a reasonable forward-looking default that preserves
the original number and gives it a real (if currently mostly
symbolic) role. Recommend keeping **1000** as the starting default,
explicitly flagged - like every other organize constant - as something
to retune once a real corpus actually reaches that scale.

---

## 4. Determining and recording "did the run complete successfully"

`organize_runs.status` (§2b) is the record. It becomes `'completed'`
**only after `reconcile()`'s full persistence has succeeded** - `topics`
rows, `topic_events` rows, *and* the graph's atomic
`staged_build.build_and_swap` - not merely after `organize()`'s
clustering-and-labeling phase finishes.

This boundary is deliberate, not the earliest point that would have
"worked": it mirrors a pattern already established in this exact
pipeline. `db.set_organize_state` (the low-confidence flag) is written
"only now that reconcile has fully committed, so the signal never gets
ahead of the hierarchy it describes"
(`jarvis/modules/pipeline.py:165-168`, comment in the existing code).
The same reasoning applies here: if a run finished labeling every
cluster successfully but then crashed during the graph swap, the actual
searchable hierarchy was never durably updated - marking that run
`'completed'` would be a lie the resume logic could act on incorrectly
(e.g., deciding "nothing to do, this config version's result is already
live" when the live hierarchy is still the *previous* run's).

**No separate explicit `'failed'` state.** A crash can't run code to
mark itself failed - the only two observable states are `'completed'`
(the happy path proved it, and stamped `completed_at`) and
`'in_progress'` (everything else: an active crash, a killed process, or,
in principle, a genuinely-still-running attempt).

**Why `'in_progress'` can be safely treated as "abandoned," not "someone
else is using it right now":** the daemon serializes ingest requests
through one lock (§1a) - at most one organize/reconcile attempt is ever
actually in flight per daemon instance. So an `'in_progress'` row found
when a *new* ingest starts its rebuild check is safe to treat as broken,
never as a live concurrent writer to guard against. This holds for a
single daemon instance owning one `jarvis.db` (the entire premise of
`docs/local-server-architecture-DESIGN.md`); it would need revisiting
only if that premise ever changed (out of scope here).

**Resume logic**, extended from `pipeline._hierarchy_rebuild_reason` /
`_run_organize`:

- No `organize_runs` row in `'in_progress'` -> normal fresh run, exactly
  today's behavior, unchanged.
- An `'in_progress'` row exists, and its `config_version_id` matches the
  current one -> **resume**: rerun clustering deterministically under
  that same config, skip every base cluster whose fingerprint is already
  checkpointed, label only what's missing, proceed to `reconcile()` as
  normal, then mark `'completed'`.
- An `'in_progress'` row exists, but its `config_version_id` differs from
  the current one -> **restart from scratch under the new version**:
  start a *new* `organize_runs` row (new run_id/sync_id, new
  `config_version_id`); do not reuse the old run's checkpoints - they
  were computed under different parameters and may not correspond to any
  cluster this run produces. The old row and its checkpoints are simply
  left behind, unused (retention/cleanup is an open question, §5).
- A `'completed'` row exists under an old `config_version_id`, and the
  current one differs -> **do nothing now**, per the requirement as
  given. The existing, live, already-persisted hierarchy stays exactly
  as-is. The new config version has no effect until the *next* run that
  a real re-sync actually triggers (`_hierarchy_rebuild_reason`'s
  existing live-chunk-set-changed condition, unchanged) - a config
  version bump does not itself trigger a rebuild.

---

## 5. Open questions

1. **What happens when `config.py`'s live values drift from the latest
   `organize_config_versions` row without a deliberate version-bump
   step?** The requirement is explicit that versions must not change
   automatically - but doesn't say what to do if someone edits
   `config.py` directly and forgets the (currently informal, undefined)
   version-bump step. Two real options: **(a)** `organize_config_versions`
   is metadata/audit-trail only; `config.py`'s literal values stay
   authoritative for actual runtime behavior; drift is a process bug,
   catchable by a test asserting `config.snapshot()` matches the latest
   recorded row. **(b)** `organize_config_versions` becomes the actual
   value clustering code reads at runtime (`config.py` becomes just the
   seed data for version 1); drift becomes structurally impossible, but
   every `config.*` read site in `clustering.py` needs replumbing to read
   from the versions table instead. I'd lean toward (a) as the smaller
   change, but it needs an explicit guard or the discipline silently
   lapses - this is the project lead's call, not mine to pick silently.

2. **RESOLVED - cross-process determinism, investigated and hardened**
   (§1d). Was: "the entire resume-by-refingerprinting design assumes
   rerunning UMAP+HDBSCAN in a brand-new process reproduces byte-identical
   base-cluster membership to the interrupted run, and nothing tests
   that." Now: investigated directly (30 real subprocess runs, both
   `_reduce_umap`/`_hdbscan_labels` in isolation and the full
   `organize()` pipeline, both the spectral-init and forced-random-init
   paths, with and without a fixed `PYTHONHASHSEED`) - **no divergence
   found in any of them**. `PYTHONHASHSEED=0` is now pinned for the
   daemon process anyway (`jarvis/cmd/daemon.py`, `jarvis/modules/daemon
   /client.py`), verified via a real relaunch mechanism and 10 real
   daemon startups, as defense-in-depth rather than a fix for an
   observed failure - the accurate framing given the investigation found
   nothing broken. `tests/test_clustering_cross_process.py` (marked
   `slow_cross_process`, `--run-slow`) makes this permanent: a future
   regression gets caught automatically rather than silently
   reintroducing an unverified assumption. See §1d for the full account.

3. **Already-placed chunks mid-interruption** (asked directly in the
   request). Answered by §1c/§4's reasoning: nothing is ever durably
   "placed" in the searchable hierarchy until `reconcile()`'s one atomic
   graph swap succeeds, so there is no state where a user-visible topic
   tree contains some chunks from an old attempt and some from a resumed
   one - `search()`/`browse()` see the last fully-completed hierarchy
   throughout, never a partial one. The only thing an interruption can
   ever cost is **re-doing LLM label calls** (mitigated by §2c's
   checkpoints), never a visibly-broken hierarchy. Confirming this
   framing is what's wanted: checkpointing here is purely a cost/time
   optimization for resuming labeling work, not a change to what's ever
   exposed to a reader mid-run.

4. **Should a live-chunk-set change *also* force a from-scratch restart,
   even under the same config version?** The requirement only specifies
   behavior when the config version differs; it's silent on the case
   where the *chunk set* organize() would cluster over has changed since
   the interrupted run (e.g., a completely separate ingest completes
   while the broken run sits `'in_progress'`, un-resumed - genuinely
   possible: the daemon's write-lock only prevents two ingests running
   *simultaneously*, not a second, unrelated ingest running to
   completion *after* the first one crashed and before anyone resumes
   it). If the live set changed, resuming "under the same config
   version" would still rerun clustering over a *different* input,
   producing different cluster fingerprints across the board -
   checkpoints would just universally miss (safe, per §2c's degrade
   path) but silently redo all the labeling work anyway, with no signal
   to anyone that the checkpoint table didn't actually help. Worth
   deciding whether that silent full-miss is acceptable, or whether a
   changed live chunk set should be detected explicitly and treated like
   a config-version mismatch (log it plainly, at least, if not treated
   identically).

5. ~~Are parent-roll-up levels' lack of checkpointing acceptable at
   scale?~~ **Resolved** (§2c, above): no - real corpora produced wide
   enough roll-up rounds that redoing them from scratch on every
   interruption was itself the dominant cost. Now checkpointed per
   parent group, same mechanism as base clusters.

6. **Checkpoint-row retention.** `organize_cluster_checkpoints` rows for
   a `'completed'` run are never read again - the completed run's actual
   output lives in `topics`/`topic_events`/the graph, not this table.
   No existing convention in this codebase clearly answers whether rows
   like this get cleaned up: `topic_events` is explicitly permanent by
   design; `sync_state`/`organize_state` are single-row and overwritten
   in place. This table doesn't cleanly match either precedent - delete
   on completion (keeps the table small, nothing needs it anymore) or
   keep indefinitely (cheap, and useful for debugging "what did attempt
   N actually label things as") is a real decision, not obvious from
   what's here. **Still open** - not deleted anywhere in the implementation
   below; rows accumulate indefinitely for now.

---

## 6. Implementation (built)

What actually got built, on branch `organize-checkpointing`, and the
real things that had to be worked out or corrected once this stopped
being a plan and became code.

### 6.1 Migration + schema

`jarvis/migrations/0012_organize_checkpointing.sql` - all three tables
from §2, unchanged from the design: `organize_config_versions`
(append-only), `organize_runs` (`id` = the existing sync_id, `status`
CHECK'd to `'in_progress'`/`'completed'`), `organize_cluster_checkpoints`
(`PRIMARY KEY (run_id, member_fingerprint)`). Table modules follow the
existing per-table + `@read_query`/`@write_query` convention exactly
(`jarvis/internal/db/organize_config_versions.py`, `organize_runs.py`,
`organize_cluster_checkpoints.py`), re-exported through `db/__init__.py`
per the project's package-split convention.

### 6.2 Config versioning

`jarvis/modules/organize/config_versions.py` - a new, small module
(not folded into `clustering.py`, which shouldn't own config-lifecycle
concerns unrelated to clustering itself): `record_new_config_version()`
is the deliberate action §2a called for - always inserts, never wired
into any pipeline path, never called automatically. `ensure_config_version()`
is the resume-side helper the design doc's schema section didn't fully
spell out a behavior for: **what happens before any version has ever
been recorded at all?** Organize can't record a run under a
`config_version_id` that doesn't exist yet (`organize_runs.config_version_id`
has a real `REFERENCES` constraint), so *something* has to seed a first
row. Resolved by treating this as a one-time bootstrap, not a species of
the "never automatic" rule the design doc was protecting: if the table
has genuinely never had a row, `ensure_config_version()` inserts one
from `config.py`'s current live values and never does so again once a
single row exists. This is seeding an undefined value, not re-versioning
an existing one - it doesn't touch or weaken §5, open question #1's
still-fully-open question (what to do if `config.py`'s live values
*later* drift from an existing recorded version) - that remains
undecided exactly as documented, unaddressed by this bootstrap.

### 6.3 Per-cluster checkpointing - the one real deviation from the plan

The design doc's §1c already knew checkpointed labels couldn't be
written into `topics` directly (no durable identity yet at labeling
time) and needed their own staging table - correct, and unchanged. What
the doc didn't fully reckon with: **`organize_vectors()` is the function
that actually runs the base-cluster labeling loop, and its docstring
made an explicit, load-bearing promise - "no database... can be run
against a read-only corpus" - specifically so the debug UI's playground
could call it safely.** Checkpointing needs to read and write
`organize_cluster_checkpoints` from inside that exact loop, which is a
real, direct conflict with that promise as originally stated.

Resolved by relaxing the promise, not breaking it: `organize_vectors()`
gained two new, optional parameters, `checkpoint_conn` and `run_id`,
both defaulting to `None`. Neither is required; when neither is given,
the function's behavior - including the "no database at all" property -
is unchanged, and every existing caller (the debug UI's playground, every
test calling `organize_vectors()` directly) passes neither and sees no
difference whatsoever. Only when a caller deliberately opts in (as
`organize()` now does whenever it's given a `run_id`) does the loop touch
`organize_cluster_checkpoints` - nothing else. This is a real,
worth-flagging correction to the design doc's implicit assumption that
checkpointing could be added without touching `organize_vectors()`'s own
contract - it couldn't, because that's genuinely where the labeling loop
lives, not `organize()` (a thin wrapper). Flagging this here rather than
silently changing the docstring's promise without a record of why.

Fingerprinting itself (`clustering._fingerprint_members`) matches §2c
exactly: `sha256` of the sorted, newline-joined member chunk_ids - no
change from the plan.

**Accepted, unhandled risk: a true fingerprint collision.** A *miss* (a
checkpointed fingerprint that doesn't match any current cluster) is
handled deliberately - it falls through to a fresh LLM call, per §2c. A
true *collision* - two genuinely different member-chunk-id sets hashing
to the same SHA-256 digest - is not handled at all:
`organize_cluster_checkpoints`'s primary key is `(run_id,
member_fingerprint)`, so a second, colliding `INSERT` for the same
run_id would raise an uncaught `sqlite3.IntegrityError` rather than being
caught and resolved somehow. This is a deliberate non-decision, not an
oversight: SHA-256's collision resistance makes this probability
low enough that adding explicit handling (a fallback key scheme, a
retry-with-suffix, etc.) isn't worth the complexity for what it
protects against. If it ever somehow occurred, it would surface loudly
as a crash on that insert, not silently corrupt a label - an acceptable
failure mode for something this unlikely.

### 6.4 Run tracking + resume decision

`pipeline._resolve_organize_run()` implements §4's three-way decision
exactly as specified: no `in_progress` row (or one under a different
config version) starts fresh; one under the *same* config version
returns its own id, letting `organize()`'s checkpoint lookups find what
it already labeled. `run_ingest()` calls this once per rebuild attempt,
before minting/reusing `sync_id`, and only inserts a fresh
`organize_runs` row when `_resolve_organize_run` says this isn't a
resume. `_run_reconcile()` marks the run `'completed'` in the same place
it already writes `organize_state` - after reconcile's persistence,
including the graph swap, has fully succeeded - matching §4's discipline
with no deviation.

### 6.5 Tests

- `tests/test_organize_checkpointing.py` (fast, in-process): config
  version creation/lookup/bootstrap; a real checkpoint *hit* (organize()
  run twice under the same run_id, second run makes zero LLM calls); a
  deliberate checkpoint *miss* (a bogus fingerprint seeded in advance
  must never be picked up, and the real cluster still gets labeled
  fresh); all three resume scenarios from §4, the third one
  (config-version bump alone must never force a rerun) verified through
  a real end-to-end `run_ingest()` call, not a hand-seeded shortcut -
  hand-seeding `chunks`/`chunk_vectors` directly turned out to be
  insufficient for exercising `_hierarchy_rebuild_reason` faithfully
  (it also depends on `document_sources`-based liveness, which a raw
  `chunks` insert doesn't populate) - a real finding from writing the
  test, not a design issue, but worth recording since it's exactly the
  kind of gap a hand-rolled fixture can hide.
- `tests/test_organize_checkpointing_interrupt.py` (marked
  `slow_cross_process`, `--run-slow`): a genuine interrupt-and-resume
  proof, not a mocked one. Spawns a real subprocess labeling a
  3-base-cluster corpus with an artificially-slowed LLM stub that logs
  each call before sleeping; waits for real, externally-observable
  evidence that one cluster's label call fully completed (a second
  call's log line appearing, which - in a single-threaded process - can
  only happen after the first one's checkpoint was already committed);
  kills the process outright (`os.kill(..., SIGTERM)`, the same
  mechanism `test_daemon.py`'s real-subprocess tests already use);
  confirms exactly one checkpoint exists and the run did *not* finish
  cleanly; spawns a second, separate subprocess resuming the same
  run_id; confirms it made *exactly* two fresh LLM calls (the two
  clusters not yet checkpointed, no more, no fewer); confirms the
  original checkpoint's exact fingerprint and label survived byte-for-byte
  unchanged; and, as the strongest possible check, calls `organize()` a
  third time in-process with an LLM stub that raises if it's ever
  invoked at all - it isn't, because all three fingerprints hit.

  One real, corrected discovery while building this test: a corpus
  verified to produce N clean base clusters via a bare
  `clustering._base_cluster(matrix)` call does **not** necessarily
  produce the same result through the real `organize()` + database round
  trip - `get_all_chunk_vectors` reads rows back via `ORDER BY chunk_id`,
  a *string* sort ("c1", "c10", "c11", ... "c2", ...), not the original
  insertion order, and UMAP/HDBSCAN's result is sensitive to input row
  order at this corpus size. This doesn't threaten the cross-process
  determinism guarantee itself (both the interrupted and resumed
  subprocess read the same table through the same query and got the same
  answer as each other, confirmed) - it only means a corpus shape must be
  verified through the *real* read path it will actually run through,
  not a shortcut that skips the database. The test's corpus parameters
  were picked by doing exactly that verification directly, not guessed.

Full suite: **405 passed, 2 skipped** with `--run-slow` (the 2 remaining
skips are the pre-existing `live_api` tests, unrelated to this work) -
**403 passed, 4 skipped** without it (the 2 new `slow_cross_process`
tests correctly gated by default, alongside the pre-existing
cross-process determinism test in the same category).
