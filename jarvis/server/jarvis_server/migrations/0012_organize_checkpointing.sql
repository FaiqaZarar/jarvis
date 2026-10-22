-- Jarvis MVP — organize checkpointing + config versioning
-- See docs/organize-checkpointing-DESIGN.md §2. Three tables:
--
-- organize_config_versions: append-only, never updated or deleted (like
-- topic_events - a permanent record, not a mutable single row like
-- sync_state/organize_state). A row is inserted only by a deliberate
-- action (organize_checkpointing.record_new_config_version, or the
-- one-time bootstrap of version 1 the first time anything asks for
-- "the current version" and finds the table empty) - never auto-bumped
-- by diffing config.py's live values against the last row. "The current
-- version" is simply MAX(id) - chronological, no separate is_active flag
-- needed.
CREATE TABLE IF NOT EXISTS organize_config_versions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    params_json  TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    note         TEXT
);

-- organize_runs: one row per organize()+reconcile() attempt. `id` is the
-- existing sync_id (jarvis.modules.pipeline mints one per rebuild
-- attempt already) reused directly rather than inventing a second
-- identifier - see design doc §2b. `status` flips to 'completed' only
-- after reconcile()'s full persistence succeeds (the atomic graph swap),
-- mirroring the existing organize_state write's "only once reconcile has
-- committed" discipline - never earlier, so a signal about run outcome
-- can't get ahead of the hierarchy it describes. No separate 'failed'
-- state: a crash can't run code to mark itself failed, so the only two
-- observable states are 'completed' and 'in_progress' (which, on the
-- next ingest, is safe to treat as abandoned - see design doc §4 for why
-- the daemon's single-writer lock makes that safe).
CREATE TABLE IF NOT EXISTS organize_runs (
    id                 TEXT PRIMARY KEY,
    config_version_id  INTEGER NOT NULL REFERENCES organize_config_versions(id),
    status             TEXT NOT NULL CHECK (status IN ('in_progress', 'completed')) DEFAULT 'in_progress',
    started_at         TEXT NOT NULL,
    completed_at       TEXT
);

-- organize_cluster_checkpoints: one label per base cluster per run,
-- keyed by a content fingerprint of that cluster's member chunk_ids -
-- NOT by HDBSCAN's own transient label numbering (e.g. "base-3"), which
-- isn't the resume key here (design doc §2c/§1d): a fingerprint can only
-- ever match a cluster with *exactly* the same membership, so a resume
-- either correctly reuses a label or safely falls through to a fresh
-- LLM call - it can never attach an old label to different content.
CREATE TABLE IF NOT EXISTS organize_cluster_checkpoints (
    run_id              TEXT NOT NULL REFERENCES organize_runs(id),
    member_fingerprint  TEXT NOT NULL,
    label               TEXT NOT NULL,
    description         TEXT NOT NULL,
    created_at          TEXT NOT NULL,
    PRIMARY KEY (run_id, member_fingerprint)
);
