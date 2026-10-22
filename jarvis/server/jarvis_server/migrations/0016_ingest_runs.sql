-- Jarvis MVP — ingest_runs + ingest_run_phases (replaces ingest_progress, migration 0015)
-- ingest_progress was single-row (id = 1): only the embed phase's counts
-- for whichever ingest was current, overwritten by the next one. That
-- can't answer "how did last night's ingest end" once a newer ingest has
-- started, and it only ever covered the embed phase - see
-- pipeline.run_ingest's PHASE_* constants for the other four.
--
-- ingest_runs: one row per run (TEXT id, minted by pipeline.run_ingest -
-- same pattern as organize_runs.id reusing the pipeline's own sync_id,
-- migration 0012). 'interrupted' status (distinct from 'failed') is for
-- a row a daemon process died without ever reaching a terminal state -
-- see ingest_runs.mark_orphaned_ingest_runs_interrupted, run once at
-- daemon startup: the daemon's single-writer lock means a row still
-- 'in_progress' when a *new* daemon process starts can only belong to a
-- process that's gone, never one still legitimately running.
CREATE TABLE IF NOT EXISTS ingest_runs (
    id            TEXT PRIMARY KEY,
    connector     TEXT NOT NULL,
    root_id       TEXT NOT NULL,
    trigger       TEXT NOT NULL,
    status        TEXT NOT NULL CHECK (status IN ('in_progress', 'done', 'failed', 'cancelled', 'interrupted'))
                  DEFAULT 'in_progress',
    started_at    TEXT NOT NULL,
    ended_at      TEXT
);

-- ingest_run_phases: one row per phase per run (sync/chunk/embed/organize/
-- reconcile - pipeline.py's PHASE_* constants). A phase never reached
-- (organize/reconcile skipped on a no-op re-sync, or anything after a
-- cancelled/failed run's last phase) simply has no row - absence IS the
-- "skipped" signal, no separate status value needed for it.
--
-- total_items/processed_items/total_batches/current_batch are non-live
-- for every phase except embed today (set once at phase end, not
-- incrementally) - only embed_and_index_chunks commits per-batch mid-
-- phase, the same deliberate trade-off ingest_progress made (see that
-- migration's header): a reader on a separate connection sees real,
-- live-updating counts for embed specifically, without every phase
-- needing a per-item DB write.
CREATE TABLE IF NOT EXISTS ingest_run_phases (
    run_id            TEXT NOT NULL REFERENCES ingest_runs(id),
    phase             TEXT NOT NULL,
    status            TEXT NOT NULL CHECK (status IN ('in_progress', 'done', 'failed')) DEFAULT 'in_progress',
    total_items       INTEGER,
    processed_items   INTEGER,
    total_batches     INTEGER,
    current_batch     INTEGER,
    started_at        TEXT NOT NULL,
    updated_at        TEXT,
    ended_at          TEXT,
    PRIMARY KEY (run_id, phase)
);

DROP TABLE IF EXISTS ingest_progress;
