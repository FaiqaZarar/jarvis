-- Jarvis MVP — drop the connector allow-list CHECK constraint
-- Proactive fix, not a specific reviewer request: consistent with Step 1's
-- FK-removal reasoning (see 0001's header), applied here too.
-- SQLite can't ALTER a CHECK constraint away in place, so this rebuilds the
-- table without it. Every future connector (roadmap §11 #2: WhatsApp,
-- Telegram, Apple Notes, chat history, Sheets/Slides...) previously needed
-- a migration just to widen this list. Consistent with Step 1's FK-removal
-- reasoning: only Jarvis's own connector code ever writes this column
-- (never external input), and each connector's own tests already catch a
-- typo'd literal connector string — the schema-level CHECK wasn't earning
-- its keep for a value nothing but our own code ever sets.

CREATE TABLE document_sources_new (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id             TEXT NOT NULL,
    connector              TEXT NOT NULL,
    content_hash            TEXT NOT NULL,
    source_url               TEXT,
    author                     TEXT,
    source_created_at          TEXT,
    source_modified_at          TEXT,
    mime                          TEXT,
    source_last_seen              TEXT NOT NULL,
    tombstoned                     INTEGER NOT NULL DEFAULT 0,
    native_metadata_json             TEXT,
    source_mtime                       REAL,
    source_size                         INTEGER,
    source_inode                         TEXT,
    UNIQUE (connector, source_id)
);

INSERT INTO document_sources_new (
    id, source_id, connector, content_hash, source_url, author,
    source_created_at, source_modified_at, mime, source_last_seen,
    tombstoned, native_metadata_json, source_mtime, source_size, source_inode
)
SELECT
    id, source_id, connector, content_hash, source_url, author,
    source_created_at, source_modified_at, mime, source_last_seen,
    tombstoned, native_metadata_json, source_mtime, source_size, source_inode
FROM document_sources;

DROP TABLE document_sources;
ALTER TABLE document_sources_new RENAME TO document_sources;

CREATE INDEX IF NOT EXISTS idx_document_sources_content_hash
    ON document_sources(content_hash);
CREATE INDEX IF NOT EXISTS idx_document_sources_inode
    ON document_sources(connector, source_inode);
