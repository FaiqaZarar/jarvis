-- Jarvis MVP — sync_roots tombstoning
-- A sync_root can be superseded by a broader ancestor root's successful
-- sync (see Connector.covered_root_ids, FilesystemConnector's override) -
-- tombstone it rather than hard-delete, per
-- docs/coding-guides/database-access.md's "cheap re-appearance" write
-- convention: if the narrower root is synced again on its own later,
-- record_sync_root's upsert un-tombstones it and its original
-- first_synced_at is preserved.
ALTER TABLE sync_roots ADD COLUMN tombstoned INTEGER NOT NULL DEFAULT 0;
