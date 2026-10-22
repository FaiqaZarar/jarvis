-- Adopts LadybugDB as the source of truth for topic/chunk relationships,
-- replacing parent_of and contains. See the graph DB adoption design
-- document for full reasoning.
--
-- This is a forward migration, not an edit to 0001. Editing an
-- already-shipped migration in place is unsafe: the migration runner
-- tracks what has been applied by filename, so any database that already
-- ran the old 0001 (with parent_of/contains present) would silently skip
-- a re-edited 0001 forever, leaving that database permanently on the old
-- schema with no error until something tries to use a column that was
-- never actually added. Confirmed directly: this reproduces as
-- "no such column: children_ids" on first sync against a pre-existing
-- database. A new migration file applies correctly regardless of whether
-- a given database already has the old tables or not.

DROP TABLE IF EXISTS parent_of;
DROP TABLE IF EXISTS contains;

ALTER TABLE topics ADD COLUMN children_ids TEXT;
ALTER TABLE topics ADD COLUMN ancestor_path TEXT;
ALTER TABLE topics ADD COLUMN direct_chunk_count INTEGER;
ALTER TABLE topics ADD COLUMN subtree_chunk_count INTEGER;

ALTER TABLE chunks ADD COLUMN topic_id TEXT;
ALTER TABLE chunks ADD COLUMN topic_confidence REAL;

CREATE INDEX IF NOT EXISTS idx_chunks_topic_id
    ON chunks(topic_id);
