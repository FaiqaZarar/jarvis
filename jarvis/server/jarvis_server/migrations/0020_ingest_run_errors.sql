-- Jarvis MVP — why an ingest run failed, and which files it couldn't read
-- (docs/observability-DESIGN.md §6). Both stay on the user's machine: the
-- desktop app reads them back to explain a failure after the fact (e.g.
-- after an app restart); only counts and codes are ever sent anywhere.
--
-- ingest_runs.error_code: an errors.ErrorCode value, set only when the
-- run's status is 'failed'.
ALTER TABLE ingest_runs ADD COLUMN error_code TEXT;

-- One row per file a run couldn't read. source_id is the connector's own
-- id (an absolute path for filesystem, a file id for Drive); display_name
-- is what the user would recognise (file name / Drive title).
CREATE TABLE IF NOT EXISTS ingest_run_file_errors (
    run_id        TEXT NOT NULL REFERENCES ingest_runs(id),
    source_id     TEXT NOT NULL,
    display_name  TEXT,
    error_code    TEXT NOT NULL,
    PRIMARY KEY (run_id, source_id)
);
