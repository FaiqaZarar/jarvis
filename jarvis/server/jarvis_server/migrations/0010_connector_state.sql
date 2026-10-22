-- Jarvis MVP — connector_state (J-11 phase 2)
-- Generic per-connector key-value persistence for a small scalar that
-- needs to survive across sync runs (e.g. Drive's Changes API
-- startPageToken). Deliberately generic, not Drive-specific - see
-- docs/J11_PHASE2_DESIGN.md §3.1 for why this isn't a new column on
-- sync_state (a different, already-load-bearing single global row)
-- and isn't a Drive-specific table (the next connector needing this
-- shape, e.g. Notion, shouldn't need its own migration to get it).
CREATE TABLE IF NOT EXISTS connector_state (
    connector   TEXT NOT NULL,
    key         TEXT NOT NULL,
    value       TEXT,
    updated_at  TEXT NOT NULL,
    PRIMARY KEY (connector, key)
);
