-- Jarvis MVP — embedding + keyword indexes (J-06)
-- See mvp-spec.md §9 (embedder recommendation, non-binding) for context.
--
-- chunk_vectors: one row per embedded chunk, sqlite-vec virtual table.
-- Populated lazily by embed_and_index_chunks() - a chunk_id present here
-- means "already embedded," which is exactly the cache-check J-06's
-- design relies on to avoid re-calling the embedding API for unchanged
-- content.
CREATE VIRTUAL TABLE IF NOT EXISTS chunk_vectors USING vec0(
    chunk_id TEXT PRIMARY KEY,
    embedding FLOAT[1024]
);

-- chunks_fts: FTS5 keyword index, kept in sync alongside chunk_vectors by
-- the same embed_and_index_chunks() pass - one indexing step populates
-- both the vector and keyword sides together.
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    chunk_id UNINDEXED, text
);
