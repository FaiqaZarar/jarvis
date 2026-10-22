# Jarvis — MVP Specification

> **Status:** MVP spec, decision-level (behavior + data contracts, not code). Recommended tech
> direction is non-binding — the developer owns the *how*.
> **Audience:** the developer building the MVP.
> **Source of truth:** this document synthesizes the Wayfinder decision tickets 01–06 and connector
> feasibility research 09–11. Where a decision needs its full rationale, the owning ticket is linked.

---

## 1. Problem & differentiator

**The problem.** A person's (or team's) knowledge is scattered across many tools — cloud docs, local
files, Notion. An LLM assistant that wants to answer from *all* of it must either be handed each
source at query time (slow, lossy, capped) or rely on native connectors that **search** a source but
don't **understand the shape** of what's there.

**Jarvis.** A one-way ingestion tool that pulls content from many sources into a single, indexed,
**organized** store that an LLM queries over MCP. It runs the pull → normalize → dedup → chunk →
embed → **organize** → index pipeline once, and thereafter serves fast, structured, cited answers.

**The differentiator — index *and* organize.** Claude's native connectors index content so it can be
searched. Jarvis additionally **organizes** every chunk into an emergent topic hierarchy derived from
the *nature of the data itself* — and indexes content **and** metadata together. Two payoffs a plain
connector can't match:

- **Nothing is omitted at search time.** Content and metadata are both indexed and hybrid-ranked, so a
  hit surfaces even when the query matches metadata, not body text.
- **The corpus has a navigable structure.** An LLM can `get_hierarchy` / `browse` to reason about
  *what topics exist* and drill in — not just keyword-match blind.

The **core value test** (see §10): connect two sources, re-sync, and have an LLM answer a question —
citing data Claude's native connectors miss — while navigating the auto-built hierarchy.

---

## 2. Users, scope & non-goals

**MVP user.** A single technical user running Jarvis **locally** against their own sources. No
multi-user, no hosting, no auth infrastructure.

**North star (context, not scope).** Multi-user, cloud-hosted, team knowledge base. The MVP is a
deliberately scoped-down slice of it; every MVP decision preserves a seam toward the north star but
builds none of it.

**In scope for the MVP**

- **Primary surface: an MCP server** (§7) exposing four read tools. This is *the* product surface.
- A **thin debug/inspection UI** — read-only, for the developer/user to see sources, hierarchy, and
  sync status. Backed by `list_sources` (§7). Minimal; not a product surface.
- **Three connectors** (§8): **Google Drive**, **Drive File System** (local disk), **Notion**.
- The full pipeline (§5): pull → normalize → dedup → chunk → embed → organize → index.
- The **organization engine** (§6): emergent nested topic hierarchy that **evolves on manual re-sync**.
- **Manual re-sync only** (user-triggered, connector-level).

**Non-goals (out of scope — see §11 for where they land)**

- Multi-user, cloud hosting, privacy, auth.
- **Sync automation** (scheduled polling / webhooks) — the immediate *next* effort.
- **Conversation-driven** hierarchy evolution and conversation-as-data.
- A full **entity-relationship knowledge graph** (MVP is only *forward-compatible* with one).
- **Fact-level / atomic-claim** extraction as the organized unit (MVP unit = **chunk**).
- **Image / non-text binary** normalization (MVP handles text + clean PDFs).
- The `get_document` / `list_sources` MCP tools as *built* features (designed-for, seams reserved).

---

## 3. Core concepts / glossary

| Term | Meaning |
|---|---|
| **Source** | A connected origin of content (a Drive account, a watched local root, a Notion workspace). Identified per-item by a `source_id`. |
| **Connector** | The adapter that pulls from a Source and emits normalized Markdown + metadata. Three ship in MVP; the architecture is connector-agnostic. |
| **Document** | A unit of content, identified by the **content hash** of its normalized Markdown body. Content-addressed: identical content from two sources is **one** Document with two provenance links. |
| **`document_sources`** | Provenance rows: one per (connector, `source_id`) pointing at a Document. Holds source-native metadata and sync bookkeeping. |
| **Normalized Markdown** | The canonical, hashed representation of a Document (§5). The one contract every connector converts *to*. |
| **Chunk** | A structure-aware, content-anchored slice of a Document's Markdown. Identified by its own **chunk hash**. The unit of retrieval **and** of organization. |
| **Embedding** | A vector for a chunk from a hosted embedder. Dimension is **fixed at store creation**. |
| **Topic** | A node in the organization hierarchy. Durable identity = **UUID + centroid + member-chunk-id set**. Has a name, description, level, and a single parent. |
| **`Unfiled`** | A reserved, first-class top-level Topic holding outlier chunks. Fully searchable; never hidden, never deleted. |
| **`CONTAINS`** | Edge Topic→Chunk, carrying a `confidence` score. A chunk has exactly **one** `CONTAINS` edge (single-membership). |
| **`PARENT_OF`** | Edge Topic→Topic. The tree backbone (strict single-parent). |
| **Organize** | The pass that clusters chunk embeddings into Topics and reconciles them against the prior hierarchy (§6). |
| **Re-sync** | A user-triggered, connector-level re-pull that detects deltas by content hash and, if the live set changed, re-runs organize. |
| **Manual re-sync** | The MVP's only sync trigger. No automation. |

---

## 4. Data model

Content-addressed and stored as a **labeled property graph** physically backed by **SQLite + a vector
column** (per ticket [03](issues/03-organization-model-grilling.md)). Choosing a property graph now
while constraining the engine to emit a *tree* keeps the KG-future seam open as a purely additive
migration (add `Entity` nodes + typed relations later) without paying a DAG's navigation cost today.

**Entities & tables**

- **`documents`** — identity = `content_hash` (SHA-256 over normalized body only). Columns:
  `content_hash` (PK), `normalized_markdown`, first-seen / ingest timestamps.
- **`document_sources`** — N per Document; one row per (connector, `source_id`). Columns: `source_id`
  (stable, persisted — the join key `list_sources` and citations key on), `connector`, `source_url`,
  `author`, source created/modified timestamps, `mime`, `source_last_seen`, tombstone flag, raw
  native-metadata JSON.
- **`chunks`** — Columns: `chunk_id` (= chunk hash), `content_hash` FK, `ordinal`, `chunk_hash`,
  **heading-path / section breadcrumb**, `embedding_ref`.
- **Graph nodes/edges** — `Topic` nodes (`id` UUID, `name`, `description`, `centroid`, `level`,
  `version`); `Chunk` nodes (`id` = content hash, `embedding`, `text`, source refs); `PARENT_OF`
  (Topic→Topic); `CONTAINS` (Topic→Chunk, with `confidence`).
- **Topic event log** — append-only, keyed by (Topic UUID, sync id), recording
  `born / rename / reparent / split / merge / tombstone`. Not full snapshots. Enough to render lineage
  and to recover stale ids (§7).

**Three metadata homes, all *outside* the hashed bytes** (so metadata churn never changes a hash):
`document_sources` (source-native), `documents` (identity + body), `chunks` (structural). **Topic
placement is not chunk metadata** — it's the `CONTAINS` edge. **User tags are post-MVP** (hierarchy is
machine-owned).

**Identity rules**

- Document identity = **content hash** (exact-match dedup only for MVP).
- `source_id` = the connector's stable per-item key (Drive `fileId`, local absolute path, Notion page
  UUID). It is a *provenance* key, distinct from Document identity.
- Topic identity = **UUID + centroid + member-set**, owned in our layer, carried across re-syncs by
  reconciliation (§6) — never something the clustering returns.

**Addendum (2026-08-23): Graph DB adopted ahead of schedule.**

This section originally specified the topic graph as "physically backed by SQLite + a vector
column," with a real graph engine framed as a post-MVP migration (§9, §11.4). Per a decision made on
2026-08-23, this has changed: **LadybugDB (an embedded graph database) is now the
source of truth for the topic hierarchy** — `PARENT_OF` and `CONTAINS` relationships live there, not
as SQL tables.

SQLite retains `documents`, `document_sources`, `chunks`, and `topic_events` unchanged, and keeps a
synced, read-only JSON cache of the graph's current structure (`topics.children_ids`, `ancestor_path`,
`direct_chunk_count`, `subtree_chunk_count`; `chunks.topic_id`, `topic_confidence`) — kept for fast,
easy inspection of the hierarchy without needing to query the graph directly. If the cache and the
graph ever disagree, the graph is authoritative.

This is a deliberate, confirmed acceleration of the post-MVP direction already named in §11.4, not a
silent architectural drift — recorded here so this section matches what is actually built.

---

## 5. Ingestion & manual re-sync pipeline

Full rationale: ticket [04](issues/04-ingestion-resync-pipeline-grilling.md).

### 5.1 Pipeline stages (canonical order)

```
pull (connector) → normalize-to-MD → document-hash + dedup → chunk → chunk-hash + embed
  → organize (cluster chunk embeddings into Topics — §6) → index
```

Chunk **precedes** organize (organize consumes chunk embeddings). Per-document up to organize;
organize is a single global pass.

- **pull** — connector fetches an item and emits Markdown via its own converter (§8). Metadata captured
  here.
- **normalize-to-MD** — the canonical pre-hash pass: Unicode **NFC** → `\n` endings + strip BOM → strip
  trailing whitespace → collapse 3+ blank lines → single trailing `\n`. **Fenced-code and table
  interiors are protected** from the collapse. No global heading-style canonicalization — trust each
  connector's serializer.
- **document-hash + dedup** — SHA-256 over the **normalized body only** (all metadata excluded).
  `INSERT documents ON CONFLICT(content_hash) DO NOTHING`; upsert one `document_sources` row per
  (connector, `source_id`).
- **chunk** — structure-aware, **content-anchored**: split on Markdown heading boundaries, pack to
  ~**512–1024 tokens with ~10–15% overlap**, never crossing a heading. Boundaries anchored to
  structure + content (not absolute offsets) so a change in one section leaves other sections'
  chunk-hashes byte-stable. *Determinism of boundaries is the non-negotiable property; the size/overlap
  figures are dev-tunable recommendations.*
- **chunk-hash + embed** — SHA-256 per chunk's normalized text; embed only chunks whose chunk-hash is
  not already cached (embedding cache). Embedder is **hosted, behind a swappable interface**.
  **Hard constraint: embedding dimension is fixed at store creation** — changing models later = full
  re-embed.
- **organize** — §6 black box: full re-cluster + identity-reconcile over the whole **live** chunk set.
- **index** — full-text (BM25) + vector + metadata over the live chunks.

### 5.2 Manual re-sync

- **Trigger granularity: connector-level** ("re-sync my Drive"). "Re-sync all" loops connectors.
  Per-document re-sync deferred. Organize re-clusters **globally, once, after the pull**.
- **Delta detection** — content hash is the source of truth; a connector's native modified-time/etag is
  an optional fast-skip to avoid re-*downloading* an almost-certainly-unchanged body. Per
  (connector, `source_id`), three outcomes:
  - **unchanged** (hash matches) → refresh `source_last_seen`; skip chunk/embed/organize.
  - **changed** (hash differs) → content-addressed identity means this is a **new** Document.
    **Repoint-and-retire**: upsert `documents` for new hash H2; repoint the provenance row H1→H2;
    chunk+embed H2 reusing cached embeddings for unchanged chunk-hashes; if H1 now has zero live
    provenance links, retire it.
  - **vanished** (previously-seen `source_id` absent from this pull) → tombstone its provenance link.
- **Deleted-source lifecycle** — deleted content **leaves the searchable corpus** (excluded from the
  next re-cluster's input, dropped from the index) but the **append-only record is kept**
  (`document_sources` tombstoned; `documents`/chunk rows retained for lineage and cheap re-appearance
  — content-hash still cached → no re-embed if the source returns).

### 5.3 Idempotency, partial-failure, concurrency

- **Two-phase.** *Phase A — enumerate + ingest*, committed per-document, fully idempotent (content-hash
  identity + upsert provenance → reprocessing a done doc is a no-op; chunk-hash cache makes a resumed
  embed re-embed only what's uncached). *Phase B — finalize, only on a fully successful pull*:
  reconcile vanished sources, then fire organize **only if the live chunk set changed** (≥1 chunk
  added / retired / content-changed). A true no-op re-sync skips organize entirely.
- **Deletion detection requires a complete enumeration** — a partial/failed pull **never tombstones**
  (a truncated listing must not read as mass deletion). A failed pull leaves the prior hierarchy and
  corpus fully intact; just re-run.
- **Concurrency** — writes are **serialized**: one re-sync at a time (lock; a second trigger queues or
  is rejected "sync in progress"). Reads serve the last committed state throughout; Phase B swaps in
  the new hierarchy **atomically** — no partial hierarchy is ever visible to a query.

---

## 6. Organization engine — the evolving hierarchy

The crux differentiator. Full rationale: ticket
[03](issues/03-organization-model-grilling.md); technique survey: research
[01](research/01-hierarchy-techniques.md).

**Shape.** A strict **single-parent tree** of `Topic` nodes over the property-graph store. Multi-parent
(DAG) is a KG-era feature; cross-topic relatedness is served by the `related` MCP tool, never by
`PARENT_OF`. **Depth is emergent** (dense areas nest deeper), guard-railed: cap ~4–5 levels, enforce a
minimum branch fan-out (no degenerate single-child chains).

**Placement — single-membership, any level.**

- Every chunk belongs to exactly **one** Topic (a true partition). `CONTAINS` carries `confidence` so
  multi-membership is a later *relaxation*, not a schema change.
- A chunk may attach at **any** level, not just leaves. **Consequence:** a Topic has **direct** chunks
  *and* **descendant** chunks — the MCP layer must distinguish them (§7 `direct_` vs `subtree_chunk_count`).
- **Placement rule — deepest-confident node:** score the chunk against node centroids on a root→leaf
  descent; place it at the **most specific node whose centroid it is within a per-level similarity
  threshold of**. Near nothing above the root threshold → **`Unfiled`**.
- **`Unfiled`** is a reserved, first-class top-level Topic. Outliers land there, remain **fully
  searchable**, and can be promoted into real topics as the corpus grows.

**Building the hierarchy (recommended technique).** Statistical base clustering + LLM roll-up:
UMAP + **HDBSCAN** over chunk embeddings for base clusters (k-means fallback; explicit unfiled bucket
for outliers); build parent levels the **Clio way** — LLM-title each cluster, embed the titles,
re-cluster, LLM propose-then-dedupe parent names. This is what makes the tree *emergent from the data*
rather than a raw dendrogram. Compute is laptop-scale; the only recurring cost is LLM labeling
(< ~$1 per full rebuild).

**Node identity & reconciliation (the mechanism that makes organization *evolve*).**

- Identity is a layer we own: each Topic = **UUID + centroid + member-chunk-id set** (chunk ids are
  stable content hashes → a reliable cross-sync signal).
- On **every manual re-sync**: **full re-cluster** of the base partition, then **match new clusters to
  existing Topics** by a weighted blend of **centroid-cosine + membership-Jaccard** (Hungarian
  assignment above threshold τ). Matched nodes carry their UUID (and, post-MVP, user edits) forward.
  *(Full-rebuild-and-reconcile, not pure-incremental placement — incremental placement ossifies
  structure, and the differentiator is organization that **improves**.)*
- **Structural-mutation vocabulary = { born, tombstone, rename, reparent, split, merge }**, every one
  **derived** from new↔old mapping cardinality — none commanded:
  - 1:1 above τ → same node (rename if label changed; reparent if parent changed)
  - 1 old : many new → **split** · many old : 1 new → **merge**
  - old matches nothing → **tombstone** (never hard-delete) · new matches nothing → **born**
- **Re-label discipline:** only re-label a node that *materially changed* (born/split/merged, or
  member-set Jaccard vs. prior self dropped below a threshold, or centroid drifted past a threshold).
  Low temperature; pass the prior label as a "keep if still accurate" hint to fight name churn.
- **Versioning** = the append-only event log (§4), keyed by (Topic UUID, sync id).

**"Improved organization" is emergent, not an optimized score.** There is **no global quality
objective function** in MVP — improvement is simply the outcome of re-clustering a larger/changed
corpus plus identity reconciliation. *Do not build an optimizer for v1.*

**Constants** (centroid-vs-Jaccard weighting, τ, `min_cluster_size`, per-level placement threshold) are
**prototype-tuned, not guessed** — see ticket [08](issues/08-identity-reconciliation-prototype.md) and
§9.

**MVP boundary.** *In:* build the hierarchy (Part 1) + evolve it on manual re-sync (Part 2a). *Out
(seams preserved):* conversation/feedback-driven evolution (Part 2b), user editing of the hierarchy,
the full KG, and any multi-parent/DAG topics.

---

## 7. MCP interface — tool contracts

The primary product surface. Full rationale: ticket
[05](issues/05-mcp-tool-contracts-grilling.md). **The caller is an LLM**, so contracts are
token-conscious: a hit is **self-sufficient at the chunk** (full chunk text inline) but never inlines
anything *above* the chunk (that's `get_document`'s job).

### 7.1 Shared return objects (exactly two)

```
ChunkHit  = { chunk_id, text, score, confidence,          // confidence = CONTAINS placement conf.
              topic:    { id, path },                       // path = ancestor-name breadcrumb
              document: { document_id, title },
              sources:  [ { source_id, connector, title, url, path, last_synced_at } ] }

TopicNode = { id, name, path, level, description,
              direct_chunk_count, subtree_chunk_count,      // any-level placement ⇒ both counts
              children?: [ TopicNode ],
              tombstoned?, superseded_by? }                 // only on stale-id lookups
```

**Identity across the boundary:** real ids cross, no second handle space. Topic ref = its **UUID**;
chunk/doc ref = the **content hash** (`chunk_id` / `document_id`), passed back verbatim. Every topic
object also carries a human `path` (breadcrumb) for reasoning and citations.

**Provenance / citation:** multi-source is first-class and attaches at the **document** level. A
`ChunkHit` carries a `document` ref + a `sources[]` array; a doc pulled from Drive *and* Notion lists
**both** deep-links and the LLM may cite either. **`source_id` is reserved on every `sources[]` entry
in v1** (the join key a future `list_sources` uses).

### 7.2 The four v1 tools (all read-only)

- **`search(query, filters?, limit?, cursor?)` → `{ results: ChunkHit[], next_cursor? }`**
  - **Filters that ship:** `topic` (scope to a subtree by UUID or path — the hierarchy's payoff),
    `source` (connector), `date_range`, `file_type`. Applied as **hard pre-filters**; a value matching
    nothing → empty results, not an error. *(`tag` dropped — no tagging system in v1.)*
  - **Ranking:** **hybrid RRF** — semantic (vector) fused with full-text (BM25) by Reciprocal Rank
    Fusion; `score` normalized 0–1.
  - **Pagination:** cursor-based; `limit` default **10**, max **50**.
- **`get_hierarchy(root?, depth?)` → nested `TopicNode` tree.** Whole tree by default (bounded by topic
  count, ~4–5 deep); `root` = subtree from a UUID; `depth` truncates. **`Unfiled` included**, flagged
  `reserved: true`. Centroids, embeddings, version history **omitted** (internal, token-heavy).
- **`browse(topic_id, cursor?, limit?)` → node metadata + child `TopicNode`s + its `ChunkHit[]`.**
  Returns **direct-member chunks only** (subtree chunks are reached by walking children;
  `subtree_chunk_count` signals more below). Direct chunks are full `ChunkHit`s, paginated small
  (`limit` default 10); child topics always returned in full.
- **`related(id, limit?)` → neighbors, polymorphic on id kind** (chunk-hash vs topic-UUID are
  format-distinguishable). A **chunk_id** → nearest other chunks by **global vector KNN that ignores
  topic boundaries**; a **topic UUID** → nearest sibling/cousin topics by centroid similarity. Returns
  `ChunkHit[]` or `TopicNode[]` with a `similarity` field, `limit` default 10. No same-document
  exclusion (the LLM sees `document_id` and judges).

### 7.3 Sizing, errors, stale ids

- **No mid-object truncation** — chunk `text` is never clipped (it would corrupt citations); chunk size
  is bounded upstream by chunking (§5). Pagination and `depth`/`root` are the **only** size levers.
- **Cursors are opaque and single-sync-scoped** — a manual re-sync invalidates them.
- **Empty is never an error** (`{ results: [] }`). Error shape `{ error: { code, message } }`; codes:
  `not_found`, `stale_cursor` (post-re-sync → caller restarts pagination), `invalid_argument`.
- **Tombstoned topic** → not a hard error: return its `TopicNode` with `tombstoned: true` +
  best-effort `superseded_by: [topic_id…]` from the event-log lineage, so a stale topic id recovers
  gracefully. **Vanished chunk** (content changed → hash changed → chunk gone) → plain `not_found`;
  caller re-searches.

### 7.4 Designed-for-but-NOT-built (seams reserved, do not build in v1)

- **`get_document(document_id, include_chunks?=false)`** → `{ document_id, title, markdown, metadata,
  sources[], chunks? }`. The "zoom above the chunk." Slots in via the `document_id` every `ChunkHit`
  already carries — **zero change** to v1 returns.
- **`list_sources()`** → `[{ source_id, connector, title, url, status, last_synced_at,
  document_count }]`. The source registry + sync status; **doubles as the backend for the thin debug
  UI** (§8.5). Keys on the `source_id` reserved in v1.

---

## 8. Connector framework + the three MVP connectors

The pipeline is **connector-agnostic**: a connector's only job is to enumerate items and, per item,
emit **(normalized-Markdown, metadata, stable `source_id`)** plus a **change-detection** signal feeding
the two-phase re-sync (§5). Everything downstream (hash, dedup, chunk, embed, organize, index) is
shared. Connector selection: community poll, ticket
[06](issues/06-community-poll-connectors-task.md).

### 8.1 Google Drive

Research: [09](issues/09-google-drive-connector-feasibility-research.md).

- **Source → MD.** Google **Docs** via `files.export(text/markdown)` (10 MB cap). Native **`.docx`** and
  **clean PDFs** via `alt=media` download → MarkItDown / Docling (per ticket 02). **Sheets and Slides
  are out of MVP scope** *(decided §07: lossy/marginal — Sheets exports only first-tab CSV, Slides has
  no MD path; deferred to post-MVP).*
- **Scope.** **My Drive + Shared-with-me** *(decided §07)*. **Shared Drives are out of MVP scope**
  (deferred).
- **Change-detection.** The **Changes API** (`startPageToken` → `changes.list`) is the delta primitive —
  it alone surfaces tombstones (`removed` / `trashed`). `modifiedTime` is a secondary fast-skip;
  **SHA-256 still decides skip vs. repoint** (§5).
- **Auth.** Scope **`drive.readonly`** (restricted). MVP stays in OAuth **Testing mode** *(decided §07)*
  — skips CASA verification, cost = **7-day refresh-token expiry** (weekly re-auth), acceptable for a
  single-user local MVP.
- **Identity.** `source_id` = Drive **`fileId`**; resolve shortcuts to their target, set shared-drive
  flags on the provenance row (even though Shared Drives are out, Shared-with-me items may carry them),
  multi-parent is moot under content-addressing.

### 8.2 Drive File System (local disk)

Research: [10](issues/10-drive-file-system-connector-feasibility-research.md).

- **Scope & selection.** User points the connector at **watched root folder(s)**. Recurse with
  `os.walk(followlinks=False)`, **skip dotfiles**, honor `.gitignore`-style ignores (`pathspec`).
- **Source → MD.** Reuse ticket-02 conversion by **extension allow-list** (`.md`/`.txt` pass-through,
  `.docx` etc. and **clean PDFs** via MarkItDown/Docling; images/binary skipped → post-MVP). One
  FS-specific add: **robust text decoding** (UTF-8 → `charset-normalizer` → strip BOM) **before** the
  shared NFC/line-ending normalizer, so local files hash **identically** to the same content from
  other sources.
- **Change-detection.** Two-phase disk scan: `(mtime, size)` fast-path (content-hash is truth), then a
  Phase-2 sweep that tombstones vanished files **after a complete pull**. `(st_dev, st_ino)` is used
  **only as a rename signal**, never as an id.
- **Identity.** **Two keys:** `source_id` = **absolute path**; Document identity = **content-hash**.
  **Inode is never an id.**
- **Access (macOS).** No auth, but **TCC** gates Desktop / Documents / Downloads — document a one-time
  **Full Disk Access** setup, and surface `PermissionError` actionably.

### 8.3 Notion

Research: [11](issues/11-notion-connector-feasibility-research.md).

- **Source → MD (don't hand-roll).** Use the first-party **`GET /v1/pages/{id}/markdown`** — one call
  returns the page body as enhanced Markdown (replaces a recursive block-crawl / `notion-to-md`).
  Tables/toggles/callouts come back as **HTML-in-Markdown** → treat as **protected regions** in the
  ticket-02 hashing (like fenced code). **Database rows need a second call**
  (`GET /v1/pages/{id}.properties` → YAML front-matter, kept **out of the hashed body**).
- **Change-detection.** `last_edited_time` pre-filter via **`data_sources/{id}/query`** +
  **`search`** (≤100/page). Deletions via `in_trash` / 404 / absence-from-full-walk → tombstone after a
  complete pull.
- **API version.** The **2025-09-03** version split database → **data sources**
  (`/v1/databases/{id}/query` deprecated). **Pin ≥ 2025-09-03** (and a version supporting the markdown
  endpoint).
- **Auth.** **Internal integration token** (`NOTION_TOKEN`, `Bearer`), read-only, no OAuth. The
  integration sees **only explicitly-connected pages** — that *is* the discovery model.
- **Identity.** `source_id` = page **UUID**. **Canonicalize the UUID form** (Notion returns hyphenated,
  accepts un-hyphenated) or one page splits into two `source_id`s and defeats dedup.
- **Robustness.** Build the full re-walk as a **rate-limited (~3 rps), `Retry-After`-aware, resumable
  queue**.

### 8.4 Cross-connector invariant

All three converge on the **same normalized-Markdown contract** and hash **identically** for the same
logical content — that is what lets multi-source dedup (one Document, N provenance links) work across
Drive + local + Notion.

### 8.5 Thin debug / inspection UI

Read-only. Backed by **`list_sources`** (§7.4). Shows, at minimum: connected **sources** and their sync
**status** / `last_synced_at` / `document_count`; the **hierarchy** (`get_hierarchy`); and a way to
**trigger a manual re-sync** per connector. Not a product surface — a developer/operator window.

---

## 9. Recommended tech direction (non-binding)

The developer owns the *how*; these are the reactions-to-research defaults, not mandates.

- **Store:** SQLite + a vector column/extension (e.g. `sqlite-vec` / FAISS sidecar), modeled as a
  property graph (Topic/Chunk nodes, `PARENT_OF`/`CONTAINS` edges). KG future = additive migration
  (Entity nodes + typed relations; optionally Kùzu/Neo4j via LlamaIndex `PropertyGraphIndex`).

  > **Update (2026-08-23):** this recommendation has been superseded for the topic graph specifically
  > — see the §4 addendum. LadybugDB is now the live source of truth for `PARENT_OF`/`CONTAINS`, not a
  > future option.
- **Conversion:** per-connector (Drive native export / Notion markdown endpoint / MarkItDown / **Docling**
  for clean PDFs), gate scanned PDFs with a PyMuPDF text-layer check.
- **Embedder:** a hosted model (e.g. OpenAI `text-embedding-3-small` or Voyage) behind a swappable
  interface. **Fix the dimension at store creation.**
- **Clustering:** UMAP + **HDBSCAN** (k-means fallback), Clio-style LLM roll-up for parent levels,
  low-temp LLM labeling with prior-label hints.
- **Ranking:** hybrid **RRF** over BM25 (SQLite FTS5) + vector KNN.
- **Reconciliation constants** — **prototype-tune, don't guess.** Ticket
  [08](issues/08-identity-reconciliation-prototype.md) tunes on sample data: centroid-cosine vs.
  membership-Jaccard weighting, match threshold τ, `min_cluster_size`, and the per-level placement
  threshold; and reports the resulting `Unfiled` rate. **If ticket 08 lands before build, feed its
  numbers here; if not, ship the mechanism with "constants prototype-tuned" and tune during build.**

---

## 10. Acceptance criteria / core-value test

**Definition of done for the MVP:**

> Connect **two** sources → run a **manual re-sync** → an LLM, via the **MCP** server, answers a
> question **citing data that Claude's native connectors miss**, and **navigates the auto-built
> hierarchy** to do it.

Concretely, the MVP is done when all hold:

1. **Ingest.** Two connectors (e.g. Google Drive + local files, or Notion + local) each pull, normalize
   to the shared Markdown contract, dedup by content hash (identical cross-source content collapses to
   one Document with two provenance links), chunk, embed, organize, and index.
2. **Organize.** `get_hierarchy` returns a non-trivial, emergently-nested Topic tree with sensible
   LLM-generated names, an `Unfiled` node present and flagged, and both `direct_` and
   `subtree_chunk_count` populated.
3. **Retrieve + cite.** `search` returns hybrid-ranked `ChunkHit`s with full chunk text and working
   multi-source citations (deep-links per `sources[]`). At least one answer surfaces content a native
   connector would omit (metadata-matched or organization-navigated).
4. **Navigate.** `browse` / `related` let the LLM move through the hierarchy (direct chunks, child
   topics, cross-topic neighbors) rather than blind keyword search.
5. **Re-sync evolves.** A second manual re-sync after adding/changing/removing sources: unchanged docs
   skip re-embed (cache hit), changed docs repoint-and-retire, vanished docs leave search but keep
   their record, and the hierarchy **reconciles** — Topic UUIDs survive, and born/rename/reparent/
   split/merge/tombstone events fire from the mapping. A true no-op re-sync fires no organize.
6. **Robustness.** A partial/failed pull tombstones nothing and leaves the prior corpus + hierarchy
   intact. Concurrent re-syncs are serialized; reads always see a committed hierarchy.

---

## 11. Post-MVP roadmap

Ordered roughly by adjacency to the MVP. Each preserved by a seam built in v1.

1. **Sync automation** — scheduled polling / webhooks. The immediate next effort. (Drive Changes API,
   Notion `last_edited_time`, and FS scan already give the delta primitives.)
2. **More connectors** — the architecture is connector-agnostic; WhatsApp, Telegram, Apple Notes, LLM
   chat history, and Google Sheets/Slides / Shared Drives were all named as candidates.
3. **`get_document` / `list_sources`** as built MCP tools — seams already reserved (§7.4).
4. **Knowledge graph** — additive migration to the property-graph store (Entity nodes + typed
   relations); MVP is only forward-compatible.

   > **Note:** the graph-engine *mechanism* named here has already landed in the MVP itself
   > (LadybugDB, see §4 addendum) — this roadmap item now refers specifically to the remaining piece:
   > adding Entity nodes and typed relations on top of what already exists, not adopting a graph
   > engine for the first time.
5. **Conversation-driven hierarchy evolution (Part 2b) & conversation-as-data** — the hierarchy learns
   from usage, and conversations themselves become a source.
6. **Richer organized unit** — fact-level / atomic-claim extraction below the chunk.
7. **User editing of the hierarchy** — pinning/renaming/reparenting; the UUID + event-log design
   already preserves this seam.
8. **Multi-user, cloud hosting, privacy, auth** — the north star.
9. **Image / non-text binary** normalization.

---

*Synthesized from Wayfinder tickets 01–06 and connector research 09–11. Reconciliation constants
pending ticket [08](issues/08-identity-reconciliation-prototype.md) (non-blocking).*
