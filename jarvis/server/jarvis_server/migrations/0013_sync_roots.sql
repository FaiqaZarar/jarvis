-- Jarvis MVP — sync_roots
-- Tracks the folders/roots each connector has synced and when they were
-- last synced cleanly. Deliberately generic across connectors: root_id is
-- whatever identifier a connector's sync() is scoped to (a resolved
-- absolute path for filesystem, a Drive folder id for Drive) rather than
-- a filesystem- or Drive-specific table, so a future connector needs no
-- new migration to get this. Distinct from mvp-spec.md §3's "Source"
-- (the connected origin) and document_sources.source_id (a per-item
-- provenance key) - see CONTEXT.md.
CREATE TABLE IF NOT EXISTS sync_roots (
    connector         TEXT NOT NULL,
    root_id           TEXT NOT NULL,
    first_synced_at   TEXT NOT NULL,
    last_synced_at    TEXT NOT NULL,
    PRIMARY KEY (connector, root_id)
);
