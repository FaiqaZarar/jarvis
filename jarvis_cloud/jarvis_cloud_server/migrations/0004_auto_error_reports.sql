-- Automatic client-error reporting, filed as GitHub issues - deduplicated by fingerprint so the
-- same recurring error (e.g. every unapproved tester hitting Drive's
-- access_denied) rolls up into one GitHub issue instead of a fresh
-- duplicate per occurrence. Global dedup, not per-user - the motivating
-- case is many different testers hitting the identical root cause.
CREATE TABLE auto_error_reports (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    fingerprint      TEXT NOT NULL UNIQUE,
    error_type       TEXT NOT NULL,
    message          TEXT NOT NULL,
    github_issue_url TEXT,
    occurrence_count INT NOT NULL DEFAULT 1,
    first_seen_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
