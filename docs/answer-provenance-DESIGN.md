# Answer provenance

Design for showing what an answer was built from — which documents,
which topic-hierarchy branches, which query, which snippets — directly
alongside a chat answer built from a Jarvis MCP call. Planned via
`/grill-with-docs`; see `CONTEXT.md` for the term itself and how it's
distinct from **eval** (`eval/`, `docs/J14_ACCURACY_EVAL.md`).

## What this is not

The repo already has something called "eval": an offline
retrieval-accuracy harness (`eval/gold_set.md`, `scripts/run_eval.py`) —
a fixed set of queries with known-correct chunks, run to score `search`
over time. That measures search *quality*; it has nothing to do with
explaining one live answer to the end user. This document is about the
latter only. Don't merge the two.

## Constraint this design works within

Jarvis's MCP server only controls the JSON it returns from a tool call.
It has no way to place anything "below the answer in chat" itself — the
chat surface is rendered by whichever LLM client (Claude Desktop, this
Claude Code session, etc.) reads the tool's JSON response and writes a
reply. So "provenance shows up under the answer" is only achievable by
making each tool response carry a complete, self-describing provenance
payload and trusting the calling model to render it — never a
server-side UI push. Everything below is scoped to that one lever: the
response shape.

## Scope: which tools

`search` and `related` (chunk-hit results) and `browse` (its direct-
member chunks) get provenance. `get_hierarchy` returns pure topic-tree
structure with no chunk hits to attach snippets to, so it's out of
scope. `query_db` is handled separately below — it runs arbitrary SQL,
so "provenance" there means something different (see below).

## `search` / `related` / `browse`: already there

Reading `jarvis/modules/mcp_tools/models.py`'s `ChunkHit` — the shape
all three already return, one per matched chunk — it already carries:

- `document` (`DocumentRef`: `document_id`, `title`)
- `sources` (`list[SourceRef]`: `connector`, `title`, `url`/`path`,
  `last_synced_at`)
- `topic.path` — the full `" > "`-joined breadcrumb string
- `text` — the snippet itself
- `score` / `confidence` / `similarity` depending on which tool produced
  it

No schema change was needed here. The one real gap: none of the three
responses echoed back the query/id that produced them, so a consumer
had to remember its own request to build a "here's what I searched for"
line. Fixed by adding one field:

```python
class SearchResult(BaseModel):
    query: str          # NEW - echoes the search() input query
    results: list[ChunkHit]
    next_cursor: Optional[str] = None
```

`related`/`browse` don't need the equivalent addition — they're keyed
by an `id` (a chunk or topic UUID), not free text, and the target
topic/chunk they were called against is already present in what they
return (`BrowseResult.topic`, the id itself for `related`).

Implemented: `jarvis/modules/mcp_tools/models.py`,
`jarvis/modules/mcp_tools/search.py` (both `SearchResult(...)`
construction sites now pass `query=query`).

## `query_db`: a SQL view, not app-level enrichment

`query_db` runs whatever `SELECT` the caller writes across 9 tables —
the tool has no idea if a result row is even "about" a document. Two
options were considered:

- **App-level row enrichment**: post-process results — if the returned
  columns include a recognizable key (`chunk_id`, `content_hash`), look
  up and bolt on document/topic metadata. Rejected: `query_db`'s own
  docstring is explicit that it's deliberately *just SQL* against a
  documented schema, no magic. Guessing at what a result row "means"
  from its column names would break that contract.
- **A read-only SQL view** (chosen): expose a `chunk_context` view that
  pre-joins `chunks` + `documents` + `document_sources` + `topics`, with
  the topic breadcrumb pre-flattened into a plain column. The caller
  just selects/joins against it like any other table. No new Python
  logic, no guessing — it's the same "just SQL, fully joinable" model
  `query_db` already has, extended to cover the one join
  (`topics.ancestor_path`, a JSON array of ancestor ids) that today
  needs procedural code (`mcp_tools/_common.py`'s `_topic_breadcrumb()`)
  to unpack for `search`/`browse`/`related`.

```sql
-- jarvis/migrations/0014_chunk_context_view.sql
CREATE VIEW IF NOT EXISTS chunk_context AS
SELECT
    c.chunk_id, c.content_hash, c.ordinal, c.heading_path,
    d.first_seen_at AS document_first_seen_at,
    d.last_ingested_at AS document_last_ingested_at,
    ds.source_id, ds.connector, ds.title AS document_title,
    ds.source_url, ds.author, ds.source_modified_at, ds.mime,
    ds.native_metadata_json,
    t.id AS topic_id, t.name AS topic_name,
    ( SELECT group_concat(tp.name, ' > ' ORDER BY je.key)
      FROM json_each(t.ancestor_path) je
      JOIN topics tp ON tp.id = je.value
      WHERE je.value != '40000000-0000-4000-8000-000000000000'
    ) AS topic_path
FROM chunks c
JOIN documents d ON d.content_hash = c.content_hash
LEFT JOIN document_sources ds
    ON ds.content_hash = c.content_hash AND ds.tombstoned = 0
LEFT JOIN topics t ON t.id = c.topic_id;
```

`'40000000-0000-4000-8000-000000000000'` is the synthetic root topic id
(`organize/config.py`'s `ROOT_TOPIC_ID`) — excluded the same way
`_topic_breadcrumb()` excludes it, since it's a hidden wrapper node
everywhere else on the MCP surface too.

Verified directly against the real `jarvis.db` (3088 embedded chunks):
`topic_path` values match the exact breadcrumb strings `search()`
already returns for the same chunks, e.g. `"Business and Professional
Growth > Personal Development > ... > October 2020 Feelings"`.

`query_db`'s tool docstring now documents `chunk_context` as a joinable
view (schema + `topic_path` note), same section that already lists the
9 real tables.

Implemented: `jarvis/migrations/0014_chunk_context_view.sql`,
`jarvis/modules/mcp_tools/sql_query.py` (docstring only — no code
change needed; the view does the work).

## Persistence

Ephemeral only, deliberately. Provenance is returned inline in the tool
response for that one call and nowhere else — no new table, no log
file. If an audit trail across calls turns out to be needed later,
that's a separate ticket with its own design (the existing
`debug_ui/trace.py` pattern — capture-on-opt-in via a `contextvars`
recorder — would be the template), not something to build speculatively
now.

## Folder / naming

This doc lives at `docs/answer-provenance-DESIGN.md`, matching the
existing convention (`docs/desktop-app-DESIGN.md`,
`docs/local-server-architecture-DESIGN.md`, `docs/J14_ACCURACY_EVAL.md`)
rather than a new top-level folder — `docs/` already is "the repo
folder for design docs from a planning session."

## Out of scope / not decided here

- No change to `get_hierarchy`.
- No new persisted audit log.
- No change to how an LLM client is *instructed* to render the
  provenance payload it receives (tool docstrings already describe the
  fields; whether that's sufficient in practice is worth observing
  before adding anything more prescriptive).
