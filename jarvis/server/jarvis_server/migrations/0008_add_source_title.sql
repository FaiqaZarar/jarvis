-- Jarvis MVP — document_sources.title (J-07)
-- See mvp-spec.md §7.1: ChunkHit.sources[].title and ChunkHit.document.title
-- are required by the MCP search() contract, but nothing in the schema
-- captured a human-readable title before this migration - confirmed by
-- grepping the whole codebase for "title" (zero matches) before adding
-- this, not assumed missing.
--
-- Nullable: only the filesystem connector populates it as of this
-- migration (title = the filename). Drive/Notion sources stay NULL until
-- their own connectors (J-11/J-12, not yet built) populate a real title
-- from their native metadata - same "nullable until the ticket that
-- populates it exists" pattern already used for chunks.topic_id /
-- topic_confidence waiting on J-08.

ALTER TABLE document_sources ADD COLUMN title TEXT;
