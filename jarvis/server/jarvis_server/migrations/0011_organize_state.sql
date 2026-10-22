-- Jarvis MVP — organize_state (low_confidence MCP surface)
-- See docs/LOW_CONFIDENCE_DESIGN.md. The organize engine's small-corpus
-- warning (clustering._assess_corpus_confidence -> OrganizeResult
-- .low_confidence / .low_confidence_reason) is computed at ingestion time
-- and discarded - nothing persists it, so get_hierarchy(), which runs at
-- query time in a possibly separate process, has no way to report it.
--
-- Neither existing state table fits (checked, not assumed): sync_state
-- (0007) is the cursor-staleness marker for search()/browse() pagination;
-- connector_state (per-connector key/value) is for connector-scoped
-- cursors like Drive's start_page_token. low_confidence is a whole-corpus
-- signal, neither per-connector nor cursor-related, so it gets its own
-- single-purpose table rather than sharing a row's meaning with another
-- concern.
--
-- Single-row (id pinned to 1 via CHECK), same as sync_state: a caller
-- only ever needs the current value, never history. Written by
-- pipeline._run_phase_b after every real organize+reconcile run; read by
-- get_hierarchy() once per call. Absent row -> (False, None), the honest
-- default before the first Phase B run has happened.
CREATE TABLE IF NOT EXISTS organize_state (
    id                     INTEGER PRIMARY KEY CHECK (id = 1),
    low_confidence         INTEGER NOT NULL DEFAULT 0,
    low_confidence_reason  TEXT,
    updated_at             TEXT NOT NULL
);
