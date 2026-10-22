-- Jarvis MVP — sync_roots display name
-- A name to show for the root, for connectors whose root_id isn't
-- readable on its own: a Drive folder id is a random string, so the
-- Sources page showed it as the source's name. Filled in by each sync
-- (Connector.display_name); NULL for filesystem, whose path already
-- carries its name, and for Drive roots not yet re-synced.
ALTER TABLE sync_roots ADD COLUMN display_name TEXT;
