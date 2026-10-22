-- Jarvis MVP — filesystem fingerprint columns (J-04)
-- See mvp-spec.md §8.2: (mtime, size) is a fast-path skip signal (content
-- hash is still the truth for change detection); (st_dev, st_ino) is used
-- only as a rename signal, never as an id — source_id (the absolute path)
-- remains the sole identity key for a document_sources row.
--
-- Nullable: only the filesystem connector populates these; Drive/Notion
-- rows leave them NULL (spec §4 — nullable fields for data that genuinely
-- isn't always available).

ALTER TABLE document_sources ADD COLUMN source_mtime REAL;
ALTER TABLE document_sources ADD COLUMN source_size INTEGER;
ALTER TABLE document_sources ADD COLUMN source_inode TEXT;   -- "{st_dev}:{st_ino}", rename signal only

CREATE INDEX IF NOT EXISTS idx_document_sources_inode
    ON document_sources(connector, source_inode);
