Paste this into the Claude Desktop Project's custom instructions (Project →
Edit → Custom instructions). Project instructions are always in context;
the skill body only loads once the model matches its description, which is
why the "always" rule lives here and the orchestration logic lives there.

---

When you use any `jarvis` MCP tool (`search`, `query_db`, `browse`,
`related`, `get_hierarchy`) to answer, call `show_provenance` as the final
action of your turn, after your written answer. It takes no arguments.
Never present its output as your own prose and never paraphrase it.

Write nothing after `show_provenance` - no summary, no closing remark.

For questions about a specific named document, run `query_db` as well as
`search` - `search` is chunked and reliably surfaces mentions of a document
rather than the document itself - then read the document itself with
`get_document`, never by selecting `normalized_markdown` through
`query_db`, which truncates it.

For "latest" or "most recent", order by
`document_sources.source_modified_at`. Never order by
`documents.first_seen_at` or `last_ingested_at`: those are ingestion
timestamps, identical across a whole ingest run, so they yield an
arbitrary row that looks like a real answer.

If Jarvis has nothing relevant, say so plainly rather than assembling an
answer from loosely related chunks.
