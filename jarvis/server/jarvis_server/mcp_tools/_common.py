"""Shared helpers for the MCP tool layer.

Cursor codec, the topic-id discriminator, breadcrumb-path resolution, and
ChunkHit assembly - used by both search.py (J-07) and hierarchy.py (J-10),
so neither tool module has to import the other.

The cursor + `_build_chunk_hit` + `_resolve_topic_filter_ids` helpers were
lifted verbatim from search.py in J-10 step 1 (pure move). `_build_chunk_hit`
was then extended in step 2 to fill in `topic` / `confidence` from the
organize/reconcile cache (J10_DESIGN.md §2.2).
"""

import base64
import json
import sqlite3
import uuid
from typing import Optional

from jarvis_server.internal import db
from jarvis_server.internal.logging.logging_config import get_logger
from jarvis_server.mcp_tools.models import ChunkHit, DocumentRef, SourceRef, Topic
from jarvis_server.organize.config import ROOT_TOPIC_ID

logger = get_logger(__name__)


# ---------------------------------------------------------------------
# Cursor encode/decode
# ---------------------------------------------------------------------

def _encode_cursor(sync_id: Optional[str], offset: int) -> str:
    """Unsigned, base64-encoded JSON - opaque per spec §7.3, not
    tamper-proof by design (single-user, no adversarial client per
    CLAUDE.md's scope)."""
    payload = json.dumps({"sync_id": sync_id, "offset": offset})
    return base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii")


def _decode_cursor(cursor: str) -> dict:
    payload = base64.urlsafe_b64decode(cursor.encode("ascii"))
    data = json.loads(payload)
    if "sync_id" not in data or "offset" not in data:
        raise ValueError("cursor missing required fields")
    return data


# ---------------------------------------------------------------------
# Topic id resolution (UUID vs. name / hash)
# ---------------------------------------------------------------------

def _looks_like_uuid(value: str) -> bool:
    """True if `value` parses as a UUID. A chunk_id is 64 hex chars, which
    `uuid.UUID` rejects, so this cleanly separates a topic UUID from a
    chunk_id for related()'s polymorphic dispatch (J10_DESIGN.md §2 /
    locked decision 4)."""
    try:
        uuid.UUID(value)
        return True
    except (ValueError, AttributeError, TypeError):
        return False


def _resolve_topic_filter_ids(conn, topic_value: str) -> list[str]:
    """UUID -> used directly. Otherwise treated as an exact match against
    topics.name - a placeholder until real breadcrumb-path resolution
    exists (presumably once hierarchy.py/get_hierarchy is built, per its
    own docstring still a J-10 stub). Name matching is inherently
    ambiguous, and this whole filter can't be meaningfully exercised
    against real data until J-08 exists anyway - flagged in
    J07_CHECKIN_2.md, proceeding on it as a low-stakes, easily-revisable
    choice.

    If multiple topics share the name, ALL of them are used (broadest
    interpretation - avoids silently dropping results the caller probably
    wanted)."""
    try:
        uuid.UUID(topic_value)
        return [topic_value]
    except ValueError:
        return db.get_topic_ids_by_name(conn, topic_value)


# ---------------------------------------------------------------------
# Breadcrumb path
# ---------------------------------------------------------------------

def _topic_breadcrumb(conn, topic_id: str) -> str:
    """`" > "`-joined ancestor names, root-to-self, INCLUDING the node's
    own name, with the synthetic `Knowledge Base` root stripped
    (J10_DESIGN.md §2.4 / §2.1).

    Reads `topics.ancestor_path` (JSON array of ancestor UUIDs root-to-self,
    populated by graph_db/sync.py). Falls back to just the node's own name
    if the cache column is missing/blank - e.g. a tombstoned topic, which
    sync.py leaves with `ancestor_path = "[]"`."""
    try:
        row = db.get_topic(conn, topic_id)
    except sqlite3.Error:
        logger.exception("failed to read ancestor_path for topic %s", topic_id)
        raise
    if row is None:
        return ""

    path_ids: list[str] = []
    if row["ancestor_path"]:
        try:
            path_ids = [tid for tid in json.loads(row["ancestor_path"]) if tid != ROOT_TOPIC_ID]
        except (TypeError, json.JSONDecodeError):
            logger.warning("topic %s has an unparseable ancestor_path; using name only", topic_id)
            path_ids = []
    if not path_ids:
        path_ids = [topic_id] if topic_id != ROOT_TOPIC_ID else []

    # TODO(perf): one query per ancestor - fine at MVP scale, revisit if
    # breadcrumbs get slow on deep hierarchies.
    names: list[str] = []
    for tid in path_ids:
        r = db.get_topic(conn, tid)
        names.append(r["name"] if r is not None else tid)
    return " > ".join(names)


# ---------------------------------------------------------------------
# ChunkHit assembly
# ---------------------------------------------------------------------

def _build_chunk_hit(conn, chunk_id: str, score: Optional[float]) -> ChunkHit:
    chunk_row = db.get_chunk_by_id(conn, chunk_id)

    source_rows = db.get_live_sources_for_document(conn, chunk_row["content_hash"])

    sources = [
        SourceRef(
            source_id=row["source_id"],
            connector=row["connector"],
            title=row["title"],
            url=row["source_url"],
            path=row["source_id"] if row["connector"] == "filesystem" else None,
            last_synced_at=row["source_last_seen"],
            source_modified_at=row["source_modified_at"],
        )
        for row in source_rows
    ]

    # First non-null title among the chunk's linked sources (confirmed
    # design). Every document has at most one source today, so this is
    # unambiguous in practice right now - stays forward-compatible once
    # cross-source dedup involves multiple real connectors.
    document_title = next((s.title for s in sources if s.title is not None), None)

    # topic / confidence: populated once the chunk has a placement
    # (chunks.topic_id, set by the reconcile cache-sync). Both stay None
    # for an un-organized chunk. A chunk in `Unfiled` reports that topic
    # like any other. (J10_DESIGN.md §2.2)
    topic: Optional[Topic] = None
    confidence: Optional[float] = None
    if chunk_row["topic_id"] is not None:
        topic_row = db.get_topic(conn, chunk_row["topic_id"])
        if topic_row is not None:
            topic = Topic(id=chunk_row["topic_id"], path=_topic_breadcrumb(conn, chunk_row["topic_id"]))
            confidence = chunk_row["topic_confidence"]

    return ChunkHit(
        chunk_id=chunk_row["chunk_id"],
        text=chunk_row["text"],
        score=score,
        confidence=confidence,
        topic=topic,
        document=DocumentRef(document_id=chunk_row["content_hash"], title=document_title),
        sources=sources,
    )
