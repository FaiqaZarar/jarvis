-- Jarvis MVP — tool_calls: a persisted log of MCP tool calls (search,
-- get_hierarchy, browse, related, get_document, query_db) and the
-- pipeline's internal LLM call (cluster/topic labelling), for later
-- quality review from the desktop app's Stats & Activity page. Capped
-- at the most recent 10,000 rows (pruned on insert - see
-- tool_calls.insert_tool_call) since this is an operational log, not a
-- durable record anything else depends on. request/response are
-- truncated before insert (tool_calls._truncate) so one oversized
-- payload can't blow up jarvis.db.
CREATE TABLE IF NOT EXISTS tool_calls (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    call_type     TEXT NOT NULL CHECK (call_type IN ('mcp_tool', 'llm')),
    source        TEXT NOT NULL,
    request       TEXT NOT NULL,
    response      TEXT NOT NULL,
    success       INTEGER NOT NULL,
    error_message TEXT,
    started_at    TEXT NOT NULL,
    duration_ms   INTEGER
);
CREATE INDEX IF NOT EXISTS idx_tool_calls_started_at ON tool_calls (started_at DESC);
