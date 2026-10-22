# Jarvis MVP

A local-first pipeline that pulls content from connectors (filesystem, Drive, and eventually others), normalizes it into a searchable, organized store. `mvp-spec.md` §3 is the fuller product glossary; this file exists to keep terms that turned out to collide - or that the spec doesn't cover - precise.

## Language

**Source**:
A connected origin of content a connector pulls from - a Drive account, a watched local root, a Notion workspace (`mvp-spec.md` §3). A product-level concept; there is no `sources` table. Don't confuse with `source_id` or `root_id` below, which are two different, more specific things that both sometimes get shortened to "source."

**`source_id`** (column: `document_sources.source_id`):
The per-*item* provenance key within a Source - one absolute path, one Drive `fileId`, one Notion page UUID. Identifies a single document, not the Source itself.

**Sync root** (table: `sync_roots`, column: `root_id`):
The specific folder/scope a `Connector` instance's `sync()` is pointed at - a resolved absolute path for filesystem, a Drive folder id for Drive. Distinct from `source_id`: a Source (e.g. "my watched Drive folder") has exactly one `root_id`, but many `source_id`s (one per file inside it). `sync_roots` tracks when each `(connector, root_id)` pair last completed a *zero-error* sync - a partial/erroring run doesn't advance it, so "last synced" always means "last synced cleanly."

**Answer provenance** (`show_provenance`, `jarvis/modules/mcp_tools/provenance.py`; `docs/answer-provenance-DESIGN.md`, `docs/adr/0001-answer-provenance-client-rendering.md`):
The documents, topic-hierarchy paths, queries and snippets behind one `search`/`related`/`browse`/`query_db` response, shown to the end user after the answer built from it. Rendered by `show_provenance`, which the model calls at the end of a turn - no MCP surface can place content after the assistant's prose, so the call is instructed, not enforced (ADR 0001). Deliberately distinct from **eval** below - they don't overlap and shouldn't be confused.

**Receipt**: the rendered output of `show_provenance` - one turn's answer provenance as readable Markdown, grouped by document. Evidence (snippets as returned), never claim-level attribution.

**Eval** (`eval/`, `docs/J14_ACCURACY_EVAL.md`, `scripts/run_eval.py`):
The offline retrieval-accuracy test harness - a fixed gold set of queries with expected chunks, run to produce precision/recall metrics. About measuring `search` quality over time, not about explaining any one live answer. Not to be conflated with **answer provenance** above, despite both having been called "eval" in conversation at one point.

**Sources page** (desktop app; code: `views/connectors_view.py`, nav label "Connectors" today):
The desktop app's home page for adding, syncing and re-syncing Sources. "Topics" is the UI word for the **Hierarchy** page. Say "Sources page," not "ingestion screen."

**Setup health**:
Whether the pieces Jarvis needs to be usable are in place - signed in, Jarvis registered in Claude Desktop's `claude_desktop_config.json` with a still-valid path, at least one Source added. Distinct from daemon health (is the background service reachable).
