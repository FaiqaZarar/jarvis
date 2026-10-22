-- Jarvis MVP — FTS5 keyword index: add Porter stemming (issue #52)
--
-- chunks_fts (migration 0006) was created with the default `unicode61`
-- tokenizer, which does no stemming: a query for "logging" would not
-- match text containing "logs", "checkpoint" would not match
-- "checkpoints", and so on. Combined with the query builder ANDing every
-- term, a natural-language question routinely matched nothing on the
-- keyword side and search fell back to vector-only ranking.
--
-- FTS5 virtual tables cannot have their tokenizer changed in place (no
-- ALTER for `tokenize=`), so this is drop + recreate + repopulate.
--
-- Repopulation source: `chunks` JOIN `chunk_vectors`. That reproduces
-- exactly what was in chunks_fts before - embed_and_index_chunks() (J-06)
-- writes a chunks_fts row in the same loop that writes the chunk_vectors
-- row, so "has a vector" == "was in the keyword index". Rebuilding from
-- the join keeps that invariant and does not index chunks that were never
-- embedded (e.g. left behind by a failed embed run).
--
-- `porter unicode61`: the Porter stemmer wrapping the same unicode61
-- tokenizer 0006 used, so tokenisation (unicode folding, diacritic
-- stripping) is unchanged and only stemming is added.

DROP TABLE IF EXISTS chunks_fts;

CREATE VIRTUAL TABLE chunks_fts USING fts5(
    chunk_id UNINDEXED,
    text,
    tokenize = 'porter unicode61'
);

INSERT INTO chunks_fts (chunk_id, text)
SELECT c.chunk_id, c.text
FROM chunks c
JOIN chunk_vectors v ON v.chunk_id = c.chunk_id;
