-- Jarvis MVP — chunk_context view (answer-provenance, see
-- docs/answer-provenance-DESIGN.md).
--
-- query_db is deliberately just read-only SQL against the real schema -
-- no app-level enrichment step guesses at what a result row "means"
-- (sql_query.py's own docstring). Making per-chunk document metadata and
-- the topic breadcrumb joinable therefore has to be plain SQL too, not a
-- Python post-process - this view is that: it does once, declaratively,
-- what mcp_tools/_common.py's _topic_breadcrumb() already does
-- procedurally for search/browse/related (unpack topics.ancestor_path,
-- a JSON array of ancestor topic ids root-to-self, into a " > "-joined
-- name path), so a query_db caller can SELECT it like any other column
-- instead of the tool inventing a special enrichment code path.
--
-- '40000000-0000-4000-8000-000000000000' is the synthetic root topic id
-- (organize/config.py's ROOT_TOPIC_ID) - excluded from the path the same
-- way _topic_breadcrumb() excludes it, since it's a wrapper node hidden
-- from the MCP surface everywhere else, not a real topic.
CREATE VIEW IF NOT EXISTS chunk_context AS
SELECT
    c.chunk_id,
    c.content_hash,
    c.ordinal,
    c.heading_path,
    d.first_seen_at        AS document_first_seen_at,
    d.last_ingested_at     AS document_last_ingested_at,
    ds.source_id,
    ds.connector,
    ds.title                AS document_title,
    ds.source_url,
    ds.author,
    ds.source_modified_at,
    ds.mime,
    ds.native_metadata_json,
    t.id                     AS topic_id,
    t.name                   AS topic_name,
    (
        SELECT group_concat(tp.name, ' > ' ORDER BY je.key)
        FROM json_each(t.ancestor_path) je
        JOIN topics tp ON tp.id = je.value
        WHERE je.value != '40000000-0000-4000-8000-000000000000'
    ) AS topic_path
FROM chunks c
JOIN documents d ON d.content_hash = c.content_hash
LEFT JOIN document_sources ds ON ds.content_hash = c.content_hash AND ds.tombstoned = 0
LEFT JOIN topics t ON t.id = c.topic_id;
