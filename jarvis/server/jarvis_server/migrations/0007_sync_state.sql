-- Jarvis MVP — sync_state (J-07)
-- See mvp-spec.md §7.3: cursors are "opaque and single-sync-scoped" - a
-- manual re-sync invalidates them (stale_cursor). Nothing in the schema
-- before this migration tracked a single, global "current sync" marker
-- to check a cursor against - confirmed by checking, not assumed
-- (grepped for sync_id/sync_run/current_sync/last_sync across the whole
-- codebase; the only prior sync_id usage is topic_events.sync_id, a
-- caller-supplied tag on topic-lineage events, not a queryable "current
-- sync" value).
--
-- Deliberately a single-row table (id is pinned to 1 via the CHECK
-- constraint) rather than an append-only log: search() only ever needs
-- "what is the current value," never sync history.
--
-- Written by embed_and_index_chunks() (J-06) - bumped after a run that
-- actually embeds something new, not on a pure cache-hit run where
-- nothing changed. Read by search() (J-07) - stamped into every issued
-- cursor, and compared against on every redeemed cursor to detect
-- staleness.
CREATE TABLE IF NOT EXISTS sync_state (
    id                 INTEGER PRIMARY KEY CHECK (id = 1),
    current_sync_id     TEXT NOT NULL
);
