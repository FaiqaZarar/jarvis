"""get_hierarchy / browse / related MCP tools (J-10).

All three are read-only and register onto the shared `server` from
server.py. They run entirely against SQLite - the `topics.*` cache
columns (`children_ids` / `ancestor_path` / `*_chunk_count`, kept current
by graph_db/sync.py), `chunks.topic_id`, `topics.centroid`, and
`chunk_vectors` - never LadybugDB directly. See mvp-spec.md §7.1-7.4 and
docs/J10_DESIGN.md.

The 8 open design questions in J10_DESIGN.md §2 were resolved per their
proposed defaults, to be reviewed later rather than confirmed
individually. The relevant ones:

  * §2.1 the synthetic `Knowledge Base` root is HIDDEN from the MCP
    surface - get_hierarchy() returns its children, breadcrumbs omit it,
    browse/related on it return invalid_argument.
  * §2.4 breadcrumb path: " > " separator, includes the node's own name.
  * §2.5 related(topic) is a global centroid KNN, no hop-limit filter -
    but the target's own ancestors and descendants are excluded, since
    the spec calls it "sibling/cousin" and an ancestor is neither
    (issue #50).
  * §2.6 `Unfiled` is excluded from related results and as a target.
  * §2.7 superseded_by: the topics column first, then the event log.
  * §2.8 depth counts levels below the requested root, root as level 0.
"""

import json
import sqlite3
import struct
from typing import Optional, Union

from jarvis_server.internal import db
from jarvis_server.internal.logging.logging_config import get_logger
from jarvis_server.mcp_tools._common import (
    _build_chunk_hit,
    _decode_cursor,
    _encode_cursor,
    _looks_like_uuid,
    _topic_breadcrumb,
)
from jarvis_server.mcp_tools.models import (
    BrowseResult,
    ChunkHit,
    ErrorDetail,
    ErrorResponse,
    HierarchyResult,
    RelatedResult,
    TopicNode,
)
from jarvis_server.mcp_tools.server import server
from jarvis_server.organize.config import ROOT_TOPIC_ID, UNFILED_TOPIC_ID

logger = get_logger(__name__)

DEFAULT_LIMIT = 10
MAX_LIMIT = 50

_RESERVED_IDS = {ROOT_TOPIC_ID, UNFILED_TOPIC_ID}


# ---------------------------------------------------------------------
# small shared helpers
# ---------------------------------------------------------------------

def _err(code: str, message: str) -> ErrorResponse:
    return ErrorResponse(error=ErrorDetail(code=code, message=message))


def _blob_to_floats(blob: Optional[bytes]) -> list[float]:
    """Inverse of embed._vector_to_blob - native float32, count inferred
    from the blob length. Empty / NULL centroid (the synthetic root, or a
    tombstoned topic) -> []."""
    if not blob:
        return []
    return list(struct.unpack(f"{len(blob) // 4}f", blob))


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


def _clamp_limit(limit: Optional[int]) -> int:
    if limit is None:
        return DEFAULT_LIMIT
    return max(1, min(limit, MAX_LIMIT))


def _ancestor_path_ids(ancestor_path: Optional[str]) -> list[str]:
    """Parse a `topics.ancestor_path` cache value (JSON array of ancestor
    UUIDs, root-to-self, includes self). Empty / unparseable -> []."""
    if not ancestor_path:
        return []
    try:
        return list(json.loads(ancestor_path))
    except (TypeError, json.JSONDecodeError):
        return []


def _child_ids(conn: sqlite3.Connection, topic_id: str) -> list[str]:
    """Direct children of `topic_id`, from the `topics.children_ids` JSON
    cache, ordered by (name, id) for a stable response."""
    try:
        row = db.get_topic(conn, topic_id)
    except sqlite3.Error:
        logger.exception("failed to read children_ids for topic %s", topic_id)
        raise
    if row is None or not row["children_ids"]:
        return []
    try:
        ids = list(json.loads(row["children_ids"]))
    except (TypeError, json.JSONDecodeError):
        logger.warning("topic %s has an unparseable children_ids cache", topic_id)
        return []
    if not ids:
        return []
    ordered = db.get_ordered_topics_by_id(conn, ids)
    return [r["id"] for r in ordered]


def _superseded_by(conn: sqlite3.Connection, topic_id: str, topic_row: sqlite3.Row) -> Optional[str]:
    """spec §7.3: best-effort tombstone lineage. The `topics.superseded_by`
    column (J-09 writes it for the merge case) first; if NULL, scan this
    topic's `tombstone` event for a `merged_into` detail (J10_DESIGN.md
    §2.7)."""
    if topic_row["superseded_by"]:
        return topic_row["superseded_by"]
    try:
        for event in db.get_events_for_topic(conn, topic_id):
            if event["event_type"] == "tombstone" and event["detail_json"]:
                detail = json.loads(event["detail_json"])
                if detail.get("merged_into"):
                    return detail["merged_into"]
    except (sqlite3.Error, TypeError, json.JSONDecodeError):
        logger.exception("failed to read tombstone lineage for topic %s", topic_id)
    return None


def _topic_node(
    conn: sqlite3.Connection,
    topic_id: str,
    *,
    max_depth: Optional[int],
    current_level: int,
) -> Optional[TopicNode]:
    """Build a TopicNode from the cache, recursing into children until
    `max_depth` levels below the first call (current_level 0). `max_depth
    is None` -> unbounded. Returns None only if the topic row vanished
    mid-walk (caller treats that as 'no such topic')."""
    try:
        row = db.get_topic(conn, topic_id)
    except sqlite3.Error:
        logger.exception("failed to load topic %s", topic_id)
        raise
    if row is None:
        return None

    tombstoned = bool(row["tombstoned"])
    node = TopicNode(
        id=row["id"],
        name=row["name"],
        path=_topic_breadcrumb(conn, topic_id),
        level=row["level"] if row["level"] is not None else 0,
        description=row["description"],
        direct_chunk_count=row["direct_chunk_count"] or 0,
        subtree_chunk_count=row["subtree_chunk_count"] or 0,
        reserved=bool(row["reserved"]),
        tombstoned=tombstoned,
        superseded_by=_superseded_by(conn, topic_id, row) if tombstoned else None,
    )

    if max_depth is not None and current_level >= max_depth:
        node.children = None
        return node

    node.children = [
        child
        for cid in _child_ids(conn, topic_id)
        if (child := _topic_node(conn, cid, max_depth=max_depth, current_level=current_level + 1))
        is not None
    ]
    return node


# ---------------------------------------------------------------------
# get_hierarchy
# ---------------------------------------------------------------------

def _get_hierarchy_impl(
    conn: sqlite3.Connection, root: Optional[str], depth: Optional[int]
) -> Union[HierarchyResult, ErrorResponse]:
    if depth is not None and depth < 0:
        return _err("invalid_argument", "depth must be >= 0")

    # Whole-corpus small-corpus warning from the last organize run. Read
    # once, attached to every HierarchyResult unchanged - it does not vary
    # by which subtree was requested. See docs/LOW_CONFIDENCE_DESIGN.md.
    low_confidence, low_confidence_reason = db.get_organize_state(conn)

    if root is None:
        # Forest: the children of the hidden synthetic root (real top-level
        # topics + `Unfiled`). Each is treated as level 0 for `depth`.
        nodes = [
            node
            for cid in _child_ids(conn, ROOT_TOPIC_ID)
            if (node := _topic_node(conn, cid, max_depth=depth, current_level=0)) is not None
        ]
        return HierarchyResult(
            topics=nodes,
            low_confidence=low_confidence,
            low_confidence_reason=low_confidence_reason,
        )

    if db.get_topic(conn, root) is None:
        return _err("not_found", f"no topic with id {root}")

    # An explicit root is honoured even if it's the synthetic node or a
    # tombstoned topic - the caller asked for it by id. Tombstoned -> the
    # node still comes back, flagged (spec §7.3 / locked decision 8).
    node = _topic_node(conn, root, max_depth=depth, current_level=0)
    if node is None:
        return _err("not_found", f"no topic with id {root}")
    return HierarchyResult(
        topics=[node],
        low_confidence=low_confidence,
        low_confidence_reason=low_confidence_reason,
    )


@server.tool()
def get_hierarchy(
    root: Optional[str] = None, depth: Optional[int] = None
) -> Union[HierarchyResult, ErrorResponse]:
    """The topic tree (spec §7.2). With no `root`, returns the top-level
    topics (the synthetic `Knowledge Base` wrapper is hidden) including
    `Unfiled` flagged `reserved: true`. `depth` counts levels below the
    requested root, the root itself being level 0: `depth=0` is just the
    node, `depth=1` adds its direct children, omitted is unbounded.
    Unknown `root` -> `not_found`; a tombstoned `root` returns its node
    with `tombstoned: true`."""
    conn = db.get_connection()
    try:
        return _get_hierarchy_impl(conn, root, depth)
    except Exception:
        logger.exception("get_hierarchy() failed unexpectedly root=%r depth=%r", root, depth)
        return _err("invalid_argument", "get_hierarchy failed due to an internal error")
    finally:
        conn.close()


# ---------------------------------------------------------------------
# browse
# ---------------------------------------------------------------------

def _browse_impl(
    conn: sqlite3.Connection, topic_id: str, cursor: Optional[str], limit: Optional[int]
) -> Union[BrowseResult, ErrorResponse]:
    if db.get_topic(conn, topic_id) is None:
        return _err("not_found", f"no topic with id {topic_id}")

    page_size = _clamp_limit(limit)
    current_sync_id = db.get_current_sync_id(conn)

    offset = 0
    if cursor is not None:
        try:
            decoded = _decode_cursor(cursor)
        except Exception:
            logger.warning("browse: malformed cursor")
            return _err("invalid_argument", "cursor could not be decoded")
        if decoded["sync_id"] != current_sync_id:
            return _err("stale_cursor", "cursor is from a prior sync; restart pagination")
        offset = decoded["offset"]

    # node + one level of full child TopicNodes
    node = _topic_node(conn, topic_id, max_depth=1, current_level=0)
    if node is None:
        return _err("not_found", f"no topic with id {topic_id}")

    # direct-member chunks only (never the subtree), still in the corpus,
    # deterministic order, paginated.
    direct_ids = [
        cid
        for cid in db.get_chunk_ids_for_topic_ordered(conn, topic_id)
        if db.chunk_has_live_source(conn, cid)
    ]
    page = direct_ids[offset:offset + page_size]
    has_more = len(direct_ids) > offset + page_size
    chunks = [_build_chunk_hit(conn, cid, None) for cid in page]
    next_cursor = _encode_cursor(current_sync_id, offset + page_size) if has_more else None

    return BrowseResult(topic=node, chunks=chunks, next_cursor=next_cursor)


@server.tool()
def browse(
    topic_id: str, cursor: Optional[str] = None, limit: Optional[int] = None
) -> Union[BrowseResult, ErrorResponse]:
    """One topic in focus (spec §7.2): its metadata, its immediate child
    topics as full `TopicNode`s, and its **direct-member** chunks only,
    paginated (`limit` default 10). Subtree chunks are never inlined -
    `topic.subtree_chunk_count` signals them. Unknown id -> `not_found`; a
    cursor from a prior sync -> `stale_cursor`."""
    conn = db.get_connection()
    try:
        return _browse_impl(conn, topic_id, cursor, limit)
    except Exception:
        logger.exception("browse() failed unexpectedly topic_id=%r", topic_id)
        return _err("invalid_argument", "browse failed due to an internal error")
    finally:
        conn.close()


# ---------------------------------------------------------------------
# related
# ---------------------------------------------------------------------

def _related_topics_impl(
    conn: sqlite3.Connection, topic_id: str, limit: int
) -> Union[RelatedResult, ErrorResponse]:
    if topic_id in _RESERVED_IDS:
        return _err("invalid_argument", "reserved topic is not a valid related() target")

    target = db.get_topic(conn, topic_id)
    if target is None:
        return _err("not_found", f"no topic with id {topic_id}")

    target_centroid = _blob_to_floats(target["centroid"])
    if not target_centroid:
        # nothing to compare against (e.g. a tombstoned topic) - empty,
        # not an error.
        return RelatedResult(topics=[])

    # "sibling/cousin" (spec §7.2): an ancestor or descendant of the target
    # is neither, so exclude the target's own lineage. Everything else is
    # unchanged - still a global centroid-cosine KNN over live non-reserved
    # topics (J10_DESIGN.md §2.5). Issue #50.
    target_lineage = set(_ancestor_path_ids(target["ancestor_path"]))   # target's ancestors + self

    candidates = db.get_related_topic_candidates(conn, topic_id)

    scored: list[tuple[str, float]] = []
    for row in candidates:
        if row["id"] in target_lineage:
            continue                                       # candidate is an ancestor of the target
        if topic_id in _ancestor_path_ids(row["ancestor_path"]):
            continue                                       # candidate is a descendant of the target
        centroid = _blob_to_floats(row["centroid"])
        if centroid:
            scored.append((row["id"], _cosine(target_centroid, centroid)))
    scored.sort(key=lambda pair: (-pair[1], pair[0]))

    topics: list[TopicNode] = []
    for tid, similarity in scored[:limit]:
        node = _topic_node(conn, tid, max_depth=0, current_level=0)
        if node is not None:
            node.similarity = similarity
            topics.append(node)
    return RelatedResult(topics=topics)


def _related_chunks_impl(
    conn: sqlite3.Connection, chunk_id: str, limit: int
) -> Union[RelatedResult, ErrorResponse]:
    if not db.chunk_exists(conn, chunk_id):
        return _err("not_found", f"no chunk with id {chunk_id}")

    target = db.get_chunk_vector(conn, chunk_id)
    if target is None:
        # chunk exists but was never embedded - no neighbours computable.
        return RelatedResult(chunks=[])
    target_vec = _blob_to_floats(target["embedding"])

    # Global KNN over every embedded chunk, ignoring topic boundaries and
    # NOT excluding same-document hits (locked decision 4). MVP corpora
    # are small, so an exact Python cosine scan is fine and sidesteps any
    # vec0 distance-metric ambiguity.
    rows = db.get_all_chunk_vectors(conn)
    scored: list[tuple[str, float]] = []
    for row in rows:
        if row["chunk_id"] == chunk_id:
            continue
        scored.append((row["chunk_id"], _cosine(target_vec, _blob_to_floats(row["embedding"]))))
    scored.sort(key=lambda pair: (-pair[1], pair[0]))

    # TODO(perf): one query per candidate - fine at MVP scale, revisit if
    # related() gets slow on large corpora.
    hits: list[ChunkHit] = []
    for cid, similarity in scored:
        if not db.chunk_has_live_source(conn, cid):
            continue
        hit = _build_chunk_hit(conn, cid, None)
        hit.similarity = similarity
        hits.append(hit)
        if len(hits) >= limit:
            break
    return RelatedResult(chunks=hits)


def _related_impl(
    conn: sqlite3.Connection, id_value: str, limit: Optional[int]
) -> Union[RelatedResult, ErrorResponse]:
    capped = _clamp_limit(limit)
    if _looks_like_uuid(id_value):
        return _related_topics_impl(conn, id_value, capped)
    return _related_chunks_impl(conn, id_value, capped)


@server.tool()
def related(id: str, limit: Optional[int] = None) -> Union[RelatedResult, ErrorResponse]:
    """Nearest neighbours (spec §7.2), polymorphic on `id`:

    * a **chunk_id** -> the most similar chunks by embedding, across the
      whole corpus, ignoring topic boundaries, keeping same-document
      hits, excluding the query chunk itself. `RelatedResult.chunks`.
    * a **topic UUID** -> the most similar sibling/cousin topics by
      centroid cosine. `RelatedResult.topics`. The target's own ancestors
      and descendants are excluded (they are neither), as are `Unfiled`
      and the synthetic root; those two are also rejected as targets
      (`invalid_argument`).

    Each result carries `similarity` (cosine, 0..1). `limit` default 10.
    Unknown / vanished id -> `not_found`."""
    conn = db.get_connection()
    try:
        return _related_impl(conn, id, limit)
    except Exception:
        logger.exception("related() failed unexpectedly id=%r", id)
        return _err("invalid_argument", "related failed due to an internal error")
    finally:
        conn.close()
