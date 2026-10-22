-- Jarvis MVP — ingest_progress (batch/item counts for the embedding step)
-- `embed_and_index_chunks` (internal/embed/embed.py) embeds every pending
-- chunk across the whole live corpus in one pass - it is deliberately
-- connector/root-agnostic (see its own docstring), so this does NOT live
-- on sync_roots (0013), which is keyed per connector+root_id: a chunk
-- embedded during one connector's ingest can belong to a different root
-- than the one that triggered the run, and sync_roots has no row that
-- would correctly own those counts.
--
-- Single-row (id pinned to 1 via CHECK), same as organize_state (0011):
-- at most one ingest is ever in flight per daemon process (server.py's
-- _WRITE_LOCK), so there is never more than one "current" progress to
-- report. Committed once per batch (embed_and_index_chunks's own
-- docstring), so a reader on a separate connection - GET /ingest/status,
-- query_db, the debug UI - sees real, live-updating counts, not just the
-- final result. `status` exists because that same per-batch commit
-- means a batch that fails partway no longer rolls the whole run back
-- (whatever embedded before the failure stays embedded); a reader must
-- be able to tell "still going" from "stopped, and did not finish"
-- rather than reading a plausible-looking but abandoned count as done.
CREATE TABLE IF NOT EXISTS ingest_progress (
    id                INTEGER PRIMARY KEY CHECK (id = 1),
    status            TEXT NOT NULL DEFAULT 'in_progress',
    total_items       INTEGER NOT NULL,
    processed_items   INTEGER NOT NULL,
    total_batches     INTEGER NOT NULL,
    current_batch     INTEGER NOT NULL,
    updated_at        TEXT NOT NULL
);
