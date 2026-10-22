-- Jarvis MVP — schema (J-02)
-- See mvp-spec.md §4 for the full data-model rationale.
--
-- Modeled as plain relational tables: the Topic/Chunk "graph" is topics +
-- edge tables, not a dedicated graph engine. This keeps J-02
-- dependency-free; a vector-column extension (sqlite-vec / FAISS sidecar)
-- is a J-06 decision once embedding actually lands — chunks.embedding_ref
-- is a placeholder until then.
--
-- No explicit foreign key constraints, and no CHECK constraint on
-- document_sources.connector: relationships and the connector allow-list
-- are enforced at the application layer (jarvis/internal/db/), not by
-- the schema. A hardcoded CHECK there would need a migration every time a
-- new connector ships (spec §11 #2 names several already planned); only
-- Jarvis's own connector code ever writes this column, so each connector's
-- own tests already catch a typo'd literal string. Join columns are still
-- indexed below for lookup performance.

-- ---------------------------------------------------------------------
-- documents: identity = content_hash (SHA-256 over normalized body only)
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS documents (
    content_hash        TEXT PRIMARY KEY,
    normalized_markdown  TEXT NOT NULL,
    first_seen_at         TEXT NOT NULL,   -- ISO8601
    last_ingested_at       TEXT NOT NULL    -- ISO8601, updated on repoint
);

-- ---------------------------------------------------------------------
-- document_sources: one row per (connector, source_id) -> a Document.
-- source_id is the connector's stable per-item key (Drive fileId,
-- absolute local path, Notion page UUID) — a provenance key, distinct
-- from Document identity.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS document_sources (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id             TEXT NOT NULL,
    connector              TEXT NOT NULL,
    content_hash            TEXT NOT NULL,
    source_url               TEXT,
    author                     TEXT,
    source_created_at          TEXT,
    source_modified_at          TEXT,
    mime                          TEXT,
    source_last_seen              TEXT NOT NULL,   -- ISO8601, refreshed each re-sync
    tombstoned                     INTEGER NOT NULL DEFAULT 0,  -- 0/1
    native_metadata_json             TEXT,           -- raw source-native metadata, JSON blob
    UNIQUE (connector, source_id)
);

CREATE INDEX IF NOT EXISTS idx_document_sources_content_hash
    ON document_sources(content_hash);

-- ---------------------------------------------------------------------
-- chunks: structure-anchored slice of a Document. chunk_id = chunk hash.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS chunks (
    chunk_id         TEXT PRIMARY KEY,   -- = chunk_hash
    content_hash       TEXT NOT NULL,
    ordinal              INTEGER NOT NULL,   -- position within the document
    chunk_hash             TEXT NOT NULL,     -- SHA-256 of the chunk's normalized text
    heading_path             TEXT,             -- section breadcrumb, e.g. "Intro > Setup"
    embedding_ref               TEXT           -- placeholder; real vector storage lands in J-06
);

CREATE INDEX IF NOT EXISTS idx_chunks_content_hash
    ON chunks(content_hash);

-- ---------------------------------------------------------------------
-- topics: nodes in the organization hierarchy.
-- Identity = UUID + centroid + member-chunk-id set (member set lives in
-- the `contains` edge table below, not duplicated here).
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS topics (
    id             TEXT PRIMARY KEY,   -- UUID
    name             TEXT NOT NULL,
    description        TEXT,
    centroid             BLOB,           -- serialized vector; placeholder until J-08
    level                  INTEGER NOT NULL DEFAULT 0,
    version                  INTEGER NOT NULL DEFAULT 1,
    reserved                   INTEGER NOT NULL DEFAULT 0,  -- 1 for the Unfiled node
    tombstoned                   INTEGER NOT NULL DEFAULT 0,
    superseded_by                  TEXT           -- comma-separated topic ids, best-effort lineage
);

-- ---------------------------------------------------------------------
-- parent_of: Topic -> Topic edges. Strict single-parent tree.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS parent_of (
    parent_topic_id     TEXT NOT NULL,
    child_topic_id        TEXT NOT NULL,
    PRIMARY KEY (child_topic_id)   -- enforces single-parent: a child has exactly one row
);

CREATE INDEX IF NOT EXISTS idx_parent_of_parent
    ON parent_of(parent_topic_id);

-- ---------------------------------------------------------------------
-- contains: Topic -> Chunk edges, with placement confidence.
-- A chunk has exactly one CONTAINS edge (single-membership in MVP).
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS contains (
    topic_id       TEXT NOT NULL,
    chunk_id         TEXT NOT NULL,
    confidence         REAL NOT NULL,
    PRIMARY KEY (chunk_id)   -- enforces single-membership
);

CREATE INDEX IF NOT EXISTS idx_contains_topic
    ON contains(topic_id);

-- ---------------------------------------------------------------------
-- topic_events: append-only event log, keyed by (Topic UUID, sync id).
-- Records born / rename / reparent / split / merge / tombstone.
-- Enough to render lineage and recover stale ids — not full snapshots.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS topic_events (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    topic_id         TEXT NOT NULL,
    sync_id            TEXT NOT NULL,
    event_type           TEXT NOT NULL CHECK (
        event_type IN ('born', 'rename', 'reparent', 'split', 'merge', 'tombstone')
    ),
    detail_json             TEXT,        -- event-specific payload, e.g. {"from": "...", "to": "..."}
    created_at                TEXT NOT NULL   -- ISO8601
);

CREATE INDEX IF NOT EXISTS idx_topic_events_topic
    ON topic_events(topic_id, sync_id);
