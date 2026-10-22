"""Shared Pydantic models for the MCP tool layer.

These are the wire shapes from mvp-spec.md §7.1-7.3 (the shared return
objects, plus the request-side filter/error shapes §7.2/§7.3 imply). They
live here rather than inside any one tool module so `search.py` and
`hierarchy.py` (J-10) both import them from a single place, instead of one
tool module owning models another tool needs.
"""

from typing import Optional

from pydantic import BaseModel


class Topic(BaseModel):
    id: str
    path: str


class TopicNode(BaseModel):
    """spec §7.1 hierarchy node. Real topic UUIDs cross the wire (no
    second handle space). Centroids / raw embeddings / version history are
    deliberately NOT exposed here (spec §7.2).

    `children` is a tri-state: a list (expanded, may be empty) vs. `None`
    (not expanded here - a `depth` cut-off in get_hierarchy, or a
    single-level browse / related result). `reserved` flags the
    engine-owned `Unfiled` node. `similarity` is populated only by
    `related()`."""

    id: str
    name: str
    path: str
    level: int
    description: Optional[str] = None
    direct_chunk_count: int = 0
    subtree_chunk_count: int = 0
    reserved: bool = False
    tombstoned: bool = False
    superseded_by: Optional[str] = None
    similarity: Optional[float] = None
    children: Optional[list["TopicNode"]] = None


class DocumentRef(BaseModel):
    document_id: str
    title: Optional[str] = None


class SourceRef(BaseModel):
    source_id: str
    connector: str
    title: Optional[str] = None
    url: Optional[str] = None
    # No dedicated "path" column exists in the schema - populated from
    # source_id for the filesystem connector, where source_id already IS
    # the absolute path (confirmed in filesystem.py's own docstring).
    # None for every other connector, since their source_id is an opaque
    # id (Drive fileId, Notion page UUID), not a real path - populating it
    # there would be inventing data, not reusing it.
    path: Optional[str] = None
    last_synced_at: str
    # When this particular document's content last changed at the
    # source, not when the connector last synced it (`last_synced_at`) -
    # from document_sources.source_modified_at, written on every ingest
    # but not surfaced anywhere until the search-result-row relative
    # timestamp (DESIGN.md §5.3, Figma parity pass). `None` for a row
    # ingested before this column existed, or a connector that never
    # populates it.
    source_modified_at: Optional[str] = None


class ChunkHit(BaseModel):
    chunk_id: str
    text: str
    # `score` is the RRF fusion score from search(). It is `None` for a
    # ChunkHit that was not produced by a relevance ranking - a browse()
    # direct-member listing, or a related() neighbour (which reports
    # `similarity` instead). See J10_DESIGN.md §2.3.
    score: Optional[float] = None
    # `topic` / `confidence` are populated from chunks.topic_id /
    # chunks.topic_confidence once the chunk has been through the organize
    # + reconcile pipeline (J-08 / J-09). Both are `None` together for a
    # chunk that has never been placed; a chunk in `Unfiled` reports that
    # topic like any other. (Completed in J-10 per J10_DESIGN.md §2.2 -
    # this also fills in the field for search() results.)
    confidence: Optional[float] = None
    topic: Optional[Topic] = None
    # `similarity` is the cosine similarity to the query, 0..1, populated
    # only by related(). `None` everywhere else.
    similarity: Optional[float] = None
    document: DocumentRef
    sources: list[SourceRef]


class SearchResult(BaseModel):
    # Echoes the search() input query - answer-provenance (see
    # docs/answer-provenance-DESIGN.md) needs the response to be
    # self-describing rather than relying on the caller to remember its
    # own request. browse()/related() don't need the equivalent: they're
    # keyed by an id, already present on the TopicNode/ChunkHit they
    # return.
    query: str
    results: list[ChunkHit]
    next_cursor: Optional[str] = None
    # Set by the stdio proxy, never the daemon. No MCP surface can force
    # the show_provenance call that clears it; this only makes skipping it
    # conspicuous rather than silent.
    provenance_pending: Optional[str] = None


class HierarchyResult(BaseModel):
    """get_hierarchy() return shape. `topics` is a forest: with no `root`
    it is the children of the synthetic `Knowledge Base` node (the wrapper
    itself is hidden, per J10_DESIGN.md §2.1); with an explicit `root` it
    is a single-element list holding that subtree.

    `low_confidence` / `low_confidence_reason` are a whole-corpus signal
    from the last organize run (small-corpus hierarchy warning), not
    per-topic - they carry the same value on every response regardless of
    `root` / `depth`. `(False, None)` before the first organize run has
    happened. See docs/LOW_CONFIDENCE_DESIGN.md."""

    topics: list[TopicNode]
    low_confidence: bool = False
    low_confidence_reason: Optional[str] = None


class BrowseResult(BaseModel):
    """browse() return shape: the node itself (its `children` populated
    one level deep with full child TopicNodes), plus its direct-member
    chunks only, paginated. Subtree chunks are never inlined -
    `topic.subtree_chunk_count` signals them."""

    topic: TopicNode
    chunks: list[ChunkHit]
    next_cursor: Optional[str] = None


class RelatedResult(BaseModel):
    """related() return shape. Exactly one of `chunks` / `topics` is set,
    depending on whether the id was a chunk_id or a topic UUID."""

    chunks: Optional[list[ChunkHit]] = None
    topics: Optional[list[TopicNode]] = None


class ErrorDetail(BaseModel):
    code: str
    message: str


class ErrorResponse(BaseModel):
    """spec §7.3's error shape: { error: { code, message } }. Returned as
    data, not raised as a Python exception - confirmed directly
    (try_mcp_error_probe.py) that a raised exception propagates through
    MCPServer.call_tool() as a generic UnexpectedToolError, losing the
    code/message distinction entirely, rather than being translated into
    this shape automatically. Returning it as structured data is the only
    way to actually deliver spec's exact error contract to the caller."""
    error: ErrorDetail


class DateRange(BaseModel):
    """ISO8601 strings, both bounds inclusive. Filters against
    document_sources.source_modified_at (real content recency), not
    documents.first_seen_at/last_ingested_at (Jarvis's own ingestion
    metadata) - a judgment call within the "keep it simple" latitude
    already given for this filter's shape."""
    from_date: Optional[str] = None
    to_date: Optional[str] = None


class SearchFilters(BaseModel):
    topic: Optional[str] = None       # topic UUID, or (fallback) exact topic name
    source: Optional[str] = None      # connector name, e.g. "filesystem"
    date_range: Optional[DateRange] = None
    file_type: Optional[str] = None   # exact match against document_sources.mime


class DocumentMatch(BaseModel):
    document_id: str
    title: Optional[str] = None
    source_modified_at: Optional[str] = None


class DocumentResult(BaseModel):
    """get_document() return shape: one document's full normalized Markdown.

    Separate from QueryResult because `query_db` truncates every cell to
    keep a broad query from dumping the corpus - a cap that makes it unable
    to return a whole document. `truncated` refers to MAX_DOCUMENT_CHARS,
    which real documents rarely reach.

    `other_matches` names the documents a title lookup also matched, newest
    first, so "the latest investor update" does not silently discard the
    older ones it ranked against.
    """

    document_id: str
    title: Optional[str] = None
    connector: Optional[str] = None
    source_url: Optional[str] = None
    path: Optional[str] = None
    source_modified_at: Optional[str] = None
    topic_path: Optional[str] = None
    markdown: str
    truncated: bool = False
    other_matches: list[DocumentMatch] = []


class ProvenanceResult(BaseModel):
    """What the accumulator renders (docs/answer-provenance-DESIGN.md).

    `show_provenance` returns the `markdown` alone - a model return type
    would reach the user as escaped JSON. The counts are here for the
    `ui://` panel and for tests.

    Markdown is the primary artifact, not a rendering of some richer
    structure: panel rendering is gated by a per-app permission prompt,
    remote feature flags and Claude Desktop's "Nonessential services"
    toggle, and shows only a stub when blocked.
    """

    markdown: str
    call_count: int
    document_count: int


class QueryResult(BaseModel):
    """query_db() return shape (jarvis_server.mcp_tools.sql_query). Lives
    here, not in sql_query.py, for the same reason every other tool
    result does: jarvis/cmd/mcp_server.py's daemon proxy needs it too,
    without importing sql_query.py itself - that module's own
    `@server.tool()` registration would double-register `query_db` onto
    the shared `server` the proxy also registers its own version on."""

    columns: list[str]
    rows: list[list[Optional[str]]]
    row_count: int
    truncated_rows: bool
    truncated_cells: bool
    # See SearchResult.provenance_pending.
    provenance_pending: Optional[str] = None


class FileTypeStat(BaseModel):
    """One row of `file_type_stats` (jarvis_server.internal.db.file_type_stats) -
    cumulative, all-time counts for one file extension."""

    extension: str
    supported: bool
    tried_count: int
    skipped_count: int
    failed_count: int
    ignored_count: int
    bytes_processed: int
    last_updated_at: str


class ProcessingTotals(BaseModel):
    total_count: int
    total_bytes: int


class FileTypeStatsResult(BaseModel):
    """GET /stats/file_types return shape - Stats & Activity page's
    processing-totals summary + per-extension table."""

    totals: ProcessingTotals
    by_extension: list[FileTypeStat]


class ToolCallRecord(BaseModel):
    """One row of `tool_calls` (jarvis_server.internal.db.tool_calls).
    `request`/`response` are JSON already serialized to text at insert
    time (see that module's `_truncate`) - left as `str` here rather than
    re-parsed, since a truncated payload is no longer valid JSON."""

    id: int
    call_type: str
    source: str
    request: str
    response: str
    success: bool
    error_message: Optional[str] = None
    started_at: str
    duration_ms: Optional[int] = None


class ToolCallHistoryResult(BaseModel):
    """GET /stats/tool_calls return shape - reverse-chronological, cursor
    (`before_id`) paginated."""

    calls: list[ToolCallRecord]


class SyncRootRecord(BaseModel):
    """One row of `sync_roots` (jarvis_server.internal.db.sync_roots) -
    already excludes tombstoned (superseded) roots."""

    connector: str
    root_id: str
    first_synced_at: str
    last_synced_at: str
    # What to call the root on screen when root_id isn't readable (a Drive
    # folder id); None for filesystem and for Drive roots not re-synced yet.
    display_name: Optional[str] = None


class SyncRootsResult(BaseModel):
    """GET /sync_roots return shape - the desktop app's Synced Folders
    panel, most recently synced first."""

    roots: list[SyncRootRecord]


# TopicNode references itself (`children: list["TopicNode"]`). Resolve the
# forward reference explicitly so import order can never leave it unbuilt.
TopicNode.model_rebuild()
