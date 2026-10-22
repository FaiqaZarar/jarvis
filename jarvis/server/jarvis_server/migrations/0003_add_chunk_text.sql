-- Jarvis MVP — chunk text storage (J-05)
-- See mvp-spec.md §5.1 / §7.1: chunk_id is a content hash of the chunk's
-- own text, not a pointer into the document (no byte offsets are stored
-- anywhere, per §5.1's "anchored to structure not absolute offsets"
-- principle) — so the chunk's text must be persisted directly, or it is
-- unrecoverable later. This is also what makes ChunkHit.text (§7.1)
-- servable without a reconstruction step at query time.

ALTER TABLE chunks ADD COLUMN text TEXT NOT NULL DEFAULT '';
