-- Jarvis MVP — file_type_stats: cumulative, all-time per-file-extension
-- processing counts for the Stats & Activity page (tried/skipped/failed/
-- ignored file counts + bytes actually processed). Accumulated across
-- every filesystem sync run rather than reset per-run - see
-- file_type_stats.upsert_file_type_stats, which adds each run's deltas
-- onto the existing row instead of overwriting it.
CREATE TABLE IF NOT EXISTS file_type_stats (
    extension        TEXT PRIMARY KEY,
    supported        INTEGER NOT NULL,
    tried_count      INTEGER NOT NULL DEFAULT 0,
    skipped_count    INTEGER NOT NULL DEFAULT 0,
    failed_count     INTEGER NOT NULL DEFAULT 0,
    ignored_count    INTEGER NOT NULL DEFAULT 0,
    bytes_processed  INTEGER NOT NULL DEFAULT 0,
    last_updated_at  TEXT NOT NULL
);
