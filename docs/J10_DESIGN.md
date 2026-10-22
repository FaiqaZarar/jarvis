# J-10 — `get_hierarchy` / `browse` / `related` MCP tools: design check-in

**Status:** findings + open questions, no implementation yet. Same
discipline as J-07/J-08/J-09.

**Ticket:** GitHub Issue #10. Spec: `mvp-spec.md` §7.1 (return objects),
§7.2 (the three tools), §7.3 (sizing / errors / stale ids), §7.4 (what
NOT to build).

---

## 1. Verified current state (read the real code, not memory)

### 1a. `hierarchy.py` is a bare stub

Exact content — 10 lines, a docstring and a logger, nothing else:

```python
"""get_hierarchy / browse / related MCP tools.

Implemented in J-10: remaining three read tools, tombstone/stale-cursor
handling. See mvp-spec.md §7.2-7.4.
"""

from jarvis.internal.logging.logging_config import get_logger

logger = get_logger(__name__)
```

No `@server.tool()` registrations. `jarvis/mcp_server.py` has the
commented-out `# from jarvis.modules.mcp_tools import hierarchy` line
waiting for exactly this.

### 1b. `topics` table — real schema

Columns, from migrations `0001` + `0005`:

| column | source migration | written by | meaning |
|---|---|---|---|
| `id` (PK) | 0001 | J-09 reconcile | topic UUID |
| `name` | 0001 | J-09 | LLM label (or reserved name) |
| `description` | 0001 | J-09 | one-line LLM description |
| `centroid` (BLOB) | 0001 | J-09 | `struct.pack("<Nf">, ...)` float32, same format as `chunk_vectors` / `embed._vector_to_blob`. Empty for the synthetic root. |
| `level` | 0001 | J-09 | 0 = synthetic root, 1 = its children, … |
| `version` | 0001 | J-09 | bumped on material change |
| `reserved` | 0001 | J-08 (`Unfiled`), J-09 (`Knowledge Base` root) | 1 = engine-owned, never matched |
| `tombstoned` | 0001 | J-09 | 1 = retired; row kept |
| `superseded_by` | 0001 | J-09 (`mark_topic_tombstoned`) | comma-separated UUID(s); populated **only for the merge case** (`merged_into`), NULL for a plain tombstone |
| `children_ids` | 0005 | `sync.py` | JSON array of direct child UUIDs |
| `ancestor_path` | 0005 | `sync.py` | JSON array of ancestor UUIDs, **root-to-self, includes self** |
| `direct_chunk_count` | 0005 | `sync.py` | count of `CONTAINS` edges |
| `subtree_chunk_count` | 0005 | `sync.py` | self + all descendants' direct chunks, de-duped |

`chunks` gained `topic_id` + `topic_confidence` (0005), written by
`sync.py::_sync_chunks` from the graph's `CONTAINS` edge (confidence =
cosine(chunk_vec, topic_centroid), set by J-09). `idx_chunks_topic_id`
exists.

### 1c. How `sync.py` populates the cache

`sync_all(sqlite_conn, graph_conn)` runs **once**, after a validated
staged-build swap (never per-write). It iterates **every** `topics` row
(`SELECT id FROM topics` — including tombstoned) and for each calls
`graph_db.get_children` / `get_ancestor_path` / `get_direct_chunk_count`
/ `get_subtree_chunk_count`, writing the four cache columns. Then it
iterates every `chunks` row and sets `topic_id`/`topic_confidence` from
`graph_db.get_chunk_topic`, or clears them to NULL if the chunk has no
current placement.

Tombstoned topics are **not written into the graph** by J-09, so
`get_children` / `get_ancestor_path` on a tombstoned id return `[]` — a
tombstoned topic ends up with `children_ids = "[]"`, `ancestor_path =
"[]"` in the cache, but its `name` / `tombstoned` / `superseded_by`
scalar columns are intact.

**Consequence for J-10:** all three tools can be built **entirely against
the SQLite cache columns** (`topics.*` + `chunks.topic_id` +
`topics.centroid` + `chunk_vectors` for `related(chunk)`). No `graph_db`
import needed — the cache is precisely what the spec §4 addendum built it
for ("fast, easy reads"). Tests seed the cache directly, exactly like
`test_search.py` already does (`_seed_topic(..., ancestor_path=...)`,
`_place_chunk_in_topic`).

### 1d. Centroid-similarity query — needs writing fresh

`grep -niE "centroid|similar|cosine|vector|distance|KNN" graph_db.py` →
**nothing.** LadybugDB's `Topic` node table has no `centroid` column at
all (`id, name, description, level, version, reserved, tombstoned,
superseded_by` only). `chunk_vectors` (vec0) is keyed by `chunk_id` and
holds chunk embeddings only — topic centroids are not in it.

So `related(topic_uuid)` centroid similarity is **new code**: load the
BLOB `centroid` for every live non-reserved topic from SQLite, `struct.
unpack` to float32, cosine against the target, top-`limit`. Topic counts
are tiny (dozens), so a plain Python scan is fine — no index needed.
`related(chunk_id)` global KNN **already exists in spirit** as
`search.py::_vector_ranked_ids`'s `candidate_ids is None` branch
(`chunk_vectors MATCH ? ORDER BY distance LIMIT`); J-10 needs the target
chunk's own vector first and to drop the self-hit.

### 1e. Reusable from `search.py`

- `_encode_cursor` / `_decode_cursor` / `_get_current_sync_id` — the
  opaque base64 `{sync_id, offset}` cursor + stale-cursor check. `browse`
  paginates its direct chunks the same way; `get_hierarchy` / `related`
  don't paginate.
- `_build_chunk_hit(conn, chunk_id, score)` — assembles a `ChunkHit` with
  `document` + `sources[]`. **Currently hard-codes `topic=None,
  confidence=None`** (see §2.2).
- `_resolve_topic_filter_ids` — UUID-vs-name discriminator (`uuid.UUID()`
  try/except). Same trick distinguishes a 64-hex `chunk_id` from a
  36-char topic UUID for `related`'s polymorphic dispatch.

These live in `search.py` today. J-10 either imports them from there or
they move to a shared `mcp_tools/_common.py`. Recommend the latter — a
small extraction, keeps `hierarchy.py` from depending on `search.py`.

### 1f. SDK mechanics (from `docs/J07_MCP_FINDINGS.md`, still valid)

Pydantic return types get full nested `$defs`/`$ref` schema generation
for free. A **recursive** model (`TopicNode.children: list["TopicNode"]`)
is standard Pydantic and serializes fine. Non-dict return types get
wrapped in `{"result": …}` in `structured_content`. `Union[Model,
ErrorResponse]` is how `search()` returns the §7.3 error shape as data
(a raised exception flattens to a generic `UnexpectedToolError`).

---

## 2. Genuinely open — needs a decision before building

### 2.1 The synthetic `Knowledge Base` root — is it visible on the MCP surface?  ⚠️ the real one

J-09 introduced a fixed reserved root topic (`ROOT_TOPIC_ID =
40000000-…`, name **"Knowledge Base"**, `reserved=1`, empty centroid) so
the hierarchy is always a strict single-parent *tree* even when organize
emits a forest. It parents every real root **and** `Unfiled`. The spec
predates it (§6 "strict single-parent tree", §7.2 "whole tree by
default") — a tree has one root, so returning it is spec-*consistent*,
but "Knowledge Base" is an engine artifact, not a data-derived topic.

Three sub-questions, and they should be answered together:

- **`get_hierarchy()` with no `root`:** return the single `Knowledge
  Base` `TopicNode` (with everything nested under it), **or** return a
  list of its children (real forest roots + `Unfiled`) and hide the
  synthetic wrapper?
- **`path` breadcrumb:** does every topic's path start with
  `"Knowledge Base / …"`, or is the synthetic root stripped from the
  breadcrumb (so a top-level real topic has `path = "Emergency Fund
  Fundamentals"`)?
- **`browse(ROOT_TOPIC_ID)` / `related(ROOT_TOPIC_ID)`:** allowed, or
  `not_found` / `invalid_argument` since it's not a real topic?

*Proposed default (flag if wrong):* **hide it.** `get_hierarchy()`
returns the list of children of `Knowledge Base`; `path` breadcrumbs omit
it; `browse`/`related` on `ROOT_TOPIC_ID` return `invalid_argument`
("reserved topic"). Rationale: the LLM caller reasons better about
"Emergency Fund Fundamentals" than "Knowledge Base / Foundational
Knowledge Areas / Emergency Fund Fundamentals", and the synthetic root
carries no information (no centroid, no description worth citing). The
one cost: `get_hierarchy()` returns a forest, not a single node — which
contradicts the literal "nested TopicNode tree" phrasing but matches
what the data actually is.

*Alternative:* keep it, flagged `reserved: true` exactly like `Unfiled`.
Simpler, spec-literal, one extra breadcrumb segment everywhere.

### 2.2 `ChunkHit.topic` / `.confidence` — complete them now?

`models.py` says verbatim: *"both are None together … until J-08 ships
and this function is updated to actually look them up."* J-08 **and** J-09
have shipped; `chunks.topic_id` / `topic_confidence` are populated on any
corpus that has been through Phase B. `browse` returns `ChunkHit[]` for a
topic's direct chunks — it would be odd for those to report `topic:
null`.

*Question:* does J-10 update `_build_chunk_hit` to populate `topic: {id,
path}` + `confidence` from the cache? That also changes **`search()`'s**
output (same helper) — arguably a good thing (completes the §7.1
contract), but it's a behaviour change to a merged tool.

*Proposed default:* yes — update `_build_chunk_hit` to look up
`chunks.topic_id` / `topic_confidence`, and when present build `topic =
{id, path}` (path per §2.4). Chunks not yet organized (or in `Unfiled`)
keep `topic: null` / or report `Unfiled`. Update `test_search.py`
expectations. Flag if this should stay scoped to `browse` only.

### 2.3 `related()`'s `similarity` field — where does it go?

§7.2: *"Returns `ChunkHit[]` or `TopicNode[]` with a `similarity`
field."* Neither model has one (`ChunkHit` has `score`, `TopicNode` has
none).

*Proposed default:* add `similarity: Optional[float] = None` to both
`ChunkHit` and `TopicNode`, populated only by `related` (cosine, 0–1).
`score` on a `related` `ChunkHit` is then either left `null` or set equal
to `similarity` — *lean toward leaving `score` null and only setting
`similarity`*, so the two fields don't imply a ranking that isn't RRF.
Flag if a dedicated `RelatedHit` wrapper is preferred instead.

### 2.4 Breadcrumb `path` format

§7.1: `path = ancestor-name breadcrumb`. The J-07 probe example was
`"Docs/Guides"`. `ancestor_path` cache is root-to-self **including
self**.

Open: **separator** (`" / "` vs `"/"` vs `" > "`) and whether the path
**includes the node's own name** (`"A / B / C"` for node C) or only
ancestors (`"A / B"`).

*Proposed default:* `" > "` separator, **include self** (so a
`ChunkHit`'s `topic.path` is the full trail to the chunk's topic), synthetic
root excluded per §2.1. e.g. `"Foundational Knowledge Areas > Emergency
Fund Fundamentals"`.

### 2.5 `related(topic_uuid)` — scope of "sibling/cousin"

§7.2: *"nearest **sibling/cousin** topics by centroid similarity."*

Open: is "sibling/cousin" a **hard filter** (only topics sharing a
grandparent / within N hops), or just a description of the *expected*
result of a global topic-centroid KNN?

*Proposed default:* global centroid KNN over all **live, non-reserved**
topics (exclude the target, `Unfiled`, `Knowledge Base`), top-`limit`.
"Sibling/cousin" is what naturally falls out — a hard tree-distance
filter would need a rationale the spec doesn't give, and with a shallow
2–3 level tree almost everything is a cousin anyway. Flag if a hop limit
is actually wanted.

### 2.6 `related` on / near `Unfiled`

`Unfiled` has a centroid (mean of its outlier members) when non-empty,
`[]` when empty. Should `related(some_topic)` be able to *return*
`Unfiled`? Should `related(UNFILED_TOPIC_ID)` work at all?

*Proposed default:* `Unfiled` is excluded from `related` results (it's
not a coherent topic — its centroid is the mean of unrelated outliers,
so "similarity" to it is meaningless), and `related(UNFILED_TOPIC_ID)`
returns `invalid_argument`. Same treatment as the synthetic root.

### 2.7 `superseded_by` source — column vs event log

§7.3 says derive `superseded_by` "from the event-log lineage."
`topics.superseded_by` **column** is populated by J-09 (`merged_into`
case only; NULL for a plain tombstone). The column is simpler; the event
log (`topic_events` where `event_type='tombstone'`) is the spec-named
source and also covers reconstructing lineage across multiple syncs.

*Proposed default:* read the `topics.superseded_by` column first; if
NULL, fall back to scanning `topic_events` for this topic's `tombstone`
event `detail_json`. Cheap, and matches both the spec intent and what
J-09 actually writes.

### 2.8 `get_hierarchy(depth)` semantics

Open: is `depth=1` "just the root node, no children", or "root + its
immediate children"? Is `depth` measured from the requested `root` or
from the true root?

*Proposed default:* `depth` counts levels **below the requested root**,
inclusive of the root itself as level 0. `depth=1` → root + direct
children, no grandchildren. `depth` omitted → unbounded (the tree is
~4–5 deep by §6). `depth=0` → just the root node, `children` omitted.

---

## 3. Spec-locked — implement as written

| # | Decision | Source |
|---|---|---|
| 1 | Three tools, **all read-only**, registered on the shared `server`. | §7.2 |
| 2 | `get_hierarchy(root?, depth?)` → nested `TopicNode`. `Unfiled` included, flagged `reserved: true`. Centroids / embeddings / version history **omitted** from the wire shape. | §7.2 |
| 3 | `browse(topic_id, cursor?, limit?)` → node metadata + child `TopicNode`s (always full) + **direct-member** `ChunkHit[]` only (paginated, `limit` default 10). Subtree chunks are *not* inlined — `subtree_chunk_count` signals them. | §7.2 |
| 4 | `related(id, limit?)` polymorphic: `chunk_id` → global vector KNN over **all** chunks, ignoring topic boundaries, **no same-document exclusion**, exclude the query chunk itself; `topic_uuid` → nearest topics by centroid similarity. `limit` default 10. | §7.2 |
| 5 | `TopicNode = { id, name, path, level, description, direct_chunk_count, subtree_chunk_count, children?, tombstoned?, superseded_by? }`. Real UUIDs cross the boundary, no second handle space. | §7.1 |
| 6 | **Empty is never an error** — `[]` / empty tree, not an error object. | §7.3 |
| 7 | Error shape `{ error: { code, message } }` as **data** (not raised); codes `not_found`, `stale_cursor`, `invalid_argument`. Unknown id → `not_found`. | §7.3 |
| 8 | **Tombstoned topic lookup is not an error** — return its `TopicNode` with `tombstoned: true` + best-effort `superseded_by`. | §7.3 |
| 9 | **Vanished chunk** (`related` on a `chunk_id` no longer in `chunks`) → plain `not_found`. | §7.3 |
| 10 | Cursors **opaque + single-sync-scoped**; a re-sync invalidates them → `stale_cursor` (reuse `search.py`'s cursor + `sync_state` check). | §7.3 |
| 11 | **No mid-object truncation** — `ChunkHit.text` never clipped. Pagination / `depth` / `root` are the only size levers. | §7.3 |
| 12 | **Do NOT build** `get_document` or `list_sources` — seams only. | §7.4 |

---

## 4. Testing approach

Same as `test_search.py`: seed `topics` + `chunks` + `chunk_vectors` +
the cache columns directly in SQLite (`_seed_topic`,
`_place_chunk_in_topic`), no LadybugDB, no network. Stub embedder for
`related(chunk)`.

- `get_hierarchy`: full tree; `root` subtree; `depth` truncation;
  `Unfiled` present + flagged; unknown root → `not_found`; tombstoned
  root → node with `tombstoned:true`.
- `browse`: direct chunks only (a subtree chunk must NOT appear); child
  topics full; chunk pagination + `next_cursor` + `stale_cursor` after a
  bumped `sync_state`; `not_found` on unknown id; direct chunks are full
  `ChunkHit`s.
- `related`: chunk→chunks KNN excludes self, crosses topics, keeps
  same-document hits; topic→topics by centroid; `similarity` populated
  and descending; unknown id → `not_found`; vanished chunk → `not_found`;
  polymorphic dispatch (64-hex vs UUID).
- Whatever §2.1 / §2.2 resolve to gets locked with a test.

---

## 5. Summary

- `hierarchy.py` is a bare stub; the cache columns J-10 needs are real,
  populated by `sync.py`, and sufficient — **J-10 is pure SQLite, no
  `graph_db`**.
- `related(topic)` centroid similarity is **new code** (nothing exists);
  `related(chunk)` KNN mirrors an existing `search.py` branch.
- **8 open questions** (§2), each with a proposed default. The one that
  actually matters is **§2.1 — whether the synthetic `Knowledge Base`
  root is visible on the MCP surface** — because it changes the shape of
  every `get_hierarchy` response and every breadcrumb, and the spec
  can't answer it (it predates the synthetic root).
- Recommend extracting the shared cursor / `_build_chunk_hit` /
  id-discriminator helpers from `search.py` into `mcp_tools/_common.py`
  before building.
