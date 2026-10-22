"""Topic hierarchy graph, backed by LadybugDB (embedded, no server).

LadybugDB is the source of truth for topic/chunk relationships -
PARENT_OF (topic tree) and CONTAINS (chunk placement). SQLite's `topics`
and `chunks` tables keep a synced, read-only cache of this structure for
fast reads (see sync.py); nothing about content or document identity
moves here.

Single-parent and single-membership are enforced by the engine itself via
relationship multiplicity constraints (MANY_ONE on PARENT_OF, ONE_MANY on
CONTAINS), not application code: a second PARENT_OF edge from an
already-placed child, or a second CONTAINS edge onto an already-placed
chunk, is rejected outright by LadybugDB at write time, not just checked
afterward.

LadybugDB allows only one process to hold a read-write connection to a
given database file at a time. This matches spec §5.3's existing
single-writer sync model - not a new constraint introduced here.
"""

import contextlib
import os
from pathlib import Path
from typing import Optional

import ladybug as lb

from jarvis_server.internal.logging.logging_config import get_logger

logger = get_logger(__name__)

# JARVIS_GRAPH_PATH override, matching jarvis/internal/db/connection.py's
# JARVIS_DB_PATH pattern exactly: the hardcoded default below resolves
# relative to this source file's location, which doesn't exist in a
# PyInstaller-frozen executable - a real, confirmed gap for the desktop
# app, not a hypothetical one. JARVIS_DAEMON_HOME is a second, coarser
# fallback for the same reason connection.py's DEFAULT_DB_PATH has one -
# see that file's comment for the precedence rationale (JARVIS_GRAPH_PATH
# wins outright when set on its own; JARVIS_DAEMON_HOME only applies when
# it isn't).
def _default_graph_path() -> Path:
    if os.environ.get("JARVIS_GRAPH_PATH"):
        return Path(os.environ["JARVIS_GRAPH_PATH"])
    if os.environ.get("JARVIS_DAEMON_HOME"):
        return Path(os.environ["JARVIS_DAEMON_HOME"]) / "jarvis_graph.lbdb"
    # Reaches the repo root - verified empirically
    # (Path(__file__).resolve().parents[5] == repo_root) - was
    # parents[3] before the server/client isolation move relocated this
    # file two levels deeper; fixed here, not left silently broken.
    return Path(__file__).resolve().parents[5] / "jarvis_graph.lbdb"


DEFAULT_GRAPH_PATH = _default_graph_path()

_SCHEMA_STATEMENTS = [
    """
    CREATE NODE TABLE IF NOT EXISTS Topic (
        id STRING PRIMARY KEY,
        name STRING,
        description STRING,
        level INT64,
        version INT64,
        reserved BOOLEAN,
        tombstoned BOOLEAN,
        superseded_by STRING
    )
    """,
    """
    CREATE NODE TABLE IF NOT EXISTS Chunk (
        id STRING PRIMARY KEY
    )
    """,
    """
    CREATE REL TABLE IF NOT EXISTS PARENT_OF (
        FROM Topic TO Topic,
        MANY_ONE
    )
    """,
    """
    CREATE REL TABLE IF NOT EXISTS CONTAINS (
        FROM Topic TO Chunk,
        confidence DOUBLE,
        ONE_MANY
    )
    """,
]


def get_connection(graph_path: Path = DEFAULT_GRAPH_PATH) -> "lb.Connection":
    """Open a read-write connection, creating the schema if this is a
    fresh database file. Only one process should hold a read-write
    connection to a given file at a time (spec §5.3's existing
    single-writer model).

    Returned connections carry a reference to their owning Database as
    `._jarvis_database`, since callers that need to move or replace the
    underlying file (see staged_build.py) must close both the connection
    and the database first - writes are not guaranteed to be flushed to
    disk otherwise: an unclosed connection's writes can be invisible to a
    fresh connection opened after the file is renamed out from under it.

    If Connection creation or schema init fails partway through, the
    Database (and Connection, if it got that far) are closed before
    re-raising - LadybugDB allows only one Database per file path at a
    time, so leaving a failed open's handle open would lock every later
    attempt to open the same path, including the natural retry."""
    database = None
    conn = None
    try:
        database = lb.Database(str(graph_path))
        conn = lb.Connection(database)
        for statement in _SCHEMA_STATEMENTS:
            conn.execute(statement)
        conn._jarvis_database = database
        return conn
    except Exception:
        logger.exception("Failed to open/initialize graph db at %s", graph_path)
        if conn is not None:
            conn.close()
        if database is not None:
            database.close()
        raise


def close_connection(conn: "lb.Connection") -> None:
    """Close a connection opened via get_connection, and its owning
    Database. Required before it's safe to move/replace/delete the
    underlying file - see get_connection's docstring."""
    try:
        database = getattr(conn, "_jarvis_database", None)
        conn.close()
        if database is not None:
            database.close()
    except Exception:
        logger.exception("Failed to close graph db connection cleanly")
        raise


def insert_topic(
    conn: "lb.Connection",
    topic_id: str,
    name: str,
    description: Optional[str] = None,
    level: int = 0,
    version: int = 1,
    reserved: bool = False,
    tombstoned: bool = False,
    superseded_by: Optional[str] = None,
) -> None:
    try:
        _evict_prepared_statement_cache(conn)  # see set_parent's docstring - same driver bug
        conn.execute(
            """
            MERGE (t:Topic {id: $id})
            SET t.name = $name, t.description = $description, t.level = $level,
                t.version = $version, t.reserved = $reserved,
                t.tombstoned = $tombstoned, t.superseded_by = $superseded_by
            """,
            {
                "id": topic_id, "name": name, "description": description,
                "level": level, "version": version, "reserved": reserved,
                "tombstoned": tombstoned, "superseded_by": superseded_by,
            },
        )
    except Exception:
        logger.exception("Failed to insert topic id=%s", topic_id)
        raise


def insert_chunk_anchor(conn: "lb.Connection", chunk_id: str) -> None:
    """Create a lightweight Chunk node purely so CONTAINS edges have
    something to attach to. No content lives here - SQLite stays the
    source of truth for the chunk's actual text."""
    try:
        _evict_prepared_statement_cache(conn)  # see set_parent's docstring - same driver bug
        conn.execute("MERGE (c:Chunk {id: $id})", {"id": chunk_id})
    except Exception:
        logger.exception("Failed to insert chunk anchor id=%s", chunk_id)
        raise


def _node_exists(conn: "lb.Connection", label: str, node_id: str) -> bool:
    result = conn.execute(f"MATCH (n:{label} {{id: $id}}) RETURN count(n)", {"id": node_id})
    return list(result)[0][0] > 0


def _evict_prepared_statement_cache(conn: "lb.Connection") -> None:
    """Work around a ladybug driver bug: its implicit prepared-statement
    cache keys on parameter *type*, not value (see ladybug's
    connection.py, `_pybind_param_signature` - a tuple of (name, type)
    pairs). set_parent()'s DELETE and CREATE run the exact same query
    text/shape on every call, just with different bound ids, so every
    call after the first reuses the same cached prepared statement -
    which, empirically, silently re-targets the first-ever-resolved node
    (`nodeOffset: 0`) instead of correctly re-resolving the MATCH against
    the new parameters. That is what actually caused the recurring
    "Node(nodeOffset: 0) has more than one neighbour in table PARENT_OF"
    crash: no jarvis code path assigns a topic two parents; the earlier
    call's plan was just silently reused for the new ids.

    Evicting the cache before each set_parent() forces a fresh prepare
    per call, mirroring the cleanup Connection.close() already does for
    the same dict (see its docstring) - this is not reaching past a
    stable public API, just doing that same eviction more often."""
    cache = getattr(conn, "_pybind_implicit_prepared_cache", None)
    if not cache:
        return
    lock = getattr(conn, "_prepared_cache_lock", None)
    with lock if lock is not None else contextlib.nullcontext():
        for prepared in cache.values():
            close_fn = getattr(prepared, "close", None)
            if callable(close_fn):
                with contextlib.suppress(Exception):
                    close_fn()
        cache.clear()


def set_parent(conn: "lb.Connection", child_topic_id: str, parent_topic_id: str) -> None:
    """Repoint (or set for the first time) a topic's parent. Deletes any
    existing outgoing PARENT_OF edge first, since re-parenting is a normal
    reconciliation event (spec §6), not an error - only a *second
    simultaneous* parent should ever be rejected by the engine.

    Both nodes are checked for existence before the delete runs: MATCH on
    a missing node returns zero rows rather than raising, so a CREATE
    chained after it would otherwise silently do nothing while the prior
    edge is already gone, leaving the child with no parent at all and no
    error to show for it."""
    try:
        _evict_prepared_statement_cache(conn)
        if not _node_exists(conn, "Topic", child_topic_id):
            raise ValueError(f"set_parent: child topic does not exist: {child_topic_id}")
        if not _node_exists(conn, "Topic", parent_topic_id):
            raise ValueError(f"set_parent: parent topic does not exist: {parent_topic_id}")

        conn.execute(
            "MATCH (c:Topic {id: $child})-[e:PARENT_OF]->() DELETE e",
            {"child": child_topic_id},
        )
        conn.execute(
            """
            MATCH (c:Topic {id: $child}), (p:Topic {id: $parent})
            CREATE (c)-[:PARENT_OF]->(p)
            """,
            {"child": child_topic_id, "parent": parent_topic_id},
        )
    except Exception:
        logger.exception(
            "Failed to set parent child=%s parent=%s", child_topic_id, parent_topic_id
        )
        raise


def place_chunk(conn: "lb.Connection", chunk_id: str, topic_id: str, confidence: float) -> None:
    """Place (or re-place) a chunk under a topic. Deletes any existing
    CONTAINS edge for this chunk first, since a chunk's placement is
    expected to change across re-syncs - only a *second simultaneous*
    placement should ever be rejected by the engine.

    Both nodes are checked for existence first, for the same reason as
    set_parent: a CREATE chained after a MATCH on a missing node silently
    does nothing rather than raising."""
    try:
        _evict_prepared_statement_cache(conn)  # see set_parent's docstring - same driver bug
        if not _node_exists(conn, "Topic", topic_id):
            raise ValueError(f"place_chunk: topic does not exist: {topic_id}")
        if not _node_exists(conn, "Chunk", chunk_id):
            raise ValueError(f"place_chunk: chunk anchor does not exist: {chunk_id}")

        conn.execute(
            "MATCH ()-[e:CONTAINS]->(c:Chunk {id: $chunk}) DELETE e",
            {"chunk": chunk_id},
        )
        conn.execute(
            """
            MATCH (t:Topic {id: $topic}), (c:Chunk {id: $chunk})
            CREATE (t)-[:CONTAINS {confidence: $confidence}]->(c)
            """,
            {"topic": topic_id, "chunk": chunk_id, "confidence": confidence},
        )
    except Exception:
        logger.exception("Failed to place chunk=%s under topic=%s", chunk_id, topic_id)
        raise


def get_children(conn: "lb.Connection", parent_topic_id: str) -> list[str]:
    try:
        result = conn.execute(
            "MATCH (c:Topic)-[:PARENT_OF]->(p:Topic {id: $parent}) RETURN c.id",
            {"parent": parent_topic_id},
        )
        return [row[0] for row in result]
    except Exception:
        logger.exception("Failed to get children of parent=%s", parent_topic_id)
        raise


def get_direct_chunks(conn: "lb.Connection", topic_id: str) -> list[tuple[str, float]]:
    try:
        result = conn.execute(
            "MATCH (t:Topic {id: $topic})-[e:CONTAINS]->(c:Chunk) RETURN c.id, e.confidence",
            {"topic": topic_id},
        )
        return [(row[0], row[1]) for row in result]
    except Exception:
        logger.exception("Failed to get direct chunks for topic=%s", topic_id)
        raise


def get_chunk_topic(conn: "lb.Connection", chunk_id: str) -> Optional[tuple[str, float]]:
    """The one topic a chunk currently belongs to, or None if unplaced."""
    try:
        result = conn.execute(
            "MATCH (t:Topic)-[e:CONTAINS]->(c:Chunk {id: $chunk}) RETURN t.id, e.confidence",
            {"chunk": chunk_id},
        )
        rows = list(result)
        return (rows[0][0], rows[0][1]) if rows else None
    except Exception:
        logger.exception("Failed to get topic for chunk=%s", chunk_id)
        raise


def get_ancestor_path(conn: "lb.Connection", topic_id: str) -> list[str]:
    """Root-to-self path of topic ids, via recursive PARENT_OF traversal.

    Ordered explicitly by hop count (length(p)) rather than relying on
    the engine's natural row order - a variable-length MATCH with no
    ORDER BY makes no ordering guarantee, so reversing an unordered
    result was an unstated assumption, not something the query itself
    enforced."""
    try:
        result = conn.execute(
            """
            MATCH p = (self:Topic {id: $id})-[:PARENT_OF*0..10]->(ancestor:Topic)
            RETURN ancestor.id, length(p) AS hops
            ORDER BY hops
            """,
            {"id": topic_id},
        )
        ids = [row[0] for row in result]
        return list(reversed(ids))
    except Exception:
        logger.exception("Failed to get ancestor path for topic=%s", topic_id)
        raise


def get_direct_chunk_count(conn: "lb.Connection", topic_id: str) -> int:
    """spec §7.1 TopicNode.direct_chunk_count."""
    try:
        result = conn.execute(
            "MATCH (t:Topic {id: $topic})-[:CONTAINS]->(c:Chunk) RETURN count(c)",
            {"topic": topic_id},
        )
        return list(result)[0][0]
    except Exception:
        logger.exception("Failed to get direct chunk count for topic=%s", topic_id)
        raise


def get_subtree_chunk_count(conn: "lb.Connection", topic_id: str) -> int:
    """spec §7.1 TopicNode.subtree_chunk_count - this topic's own direct
    chunks plus every descendant topic's direct chunks. Requires a
    recursive traversal, which is why this is cached at sync time rather
    than computed live on every MCP read."""
    try:
        result = conn.execute(
            """
            MATCH (root:Topic {id: $id})
            OPTIONAL MATCH (descendant:Topic)-[:PARENT_OF*0..10]->(root)
            OPTIONAL MATCH (descendant)-[:CONTAINS]->(c:Chunk)
            RETURN count(DISTINCT c.id)
            """,
            {"id": topic_id},
        )
        return list(result)[0][0]
    except Exception:
        logger.exception("Failed to get subtree chunk count for topic=%s", topic_id)
        raise


def get_all_topic_ids(conn: "lb.Connection") -> list[str]:
    try:
        result = conn.execute("MATCH (t:Topic) RETURN t.id")
        return [row[0] for row in result]
    except Exception:
        logger.exception("Failed to get all topic ids")
        raise


def get_all_chunk_ids(conn: "lb.Connection") -> list[str]:
    try:
        result = conn.execute("MATCH (c:Chunk) RETURN c.id")
        return [row[0] for row in result]
    except Exception:
        logger.exception("Failed to get all chunk ids")
        raise


def get_parentless_topic_ids(conn: "lb.Connection") -> list[str]:
    """Non-root topics (reserved=false) with no outgoing PARENT_OF edge -
    a completeness gap the engine's multiplicity constraints can't catch
    on their own (MANY_ONE only prevents a *second* parent, not a
    *missing* one). Used by the staged-build validation step before an
    atomic swap; see staged_build.py. The single reserved=true topic (the
    Unfiled root) is expected to have no parent and is excluded."""
    try:
        result = conn.execute(
            """
            MATCH (t:Topic)
            WHERE t.reserved = false AND NOT EXISTS { MATCH (t)-[:PARENT_OF]->() }
            RETURN t.id
            """
        )
        return [row[0] for row in result]
    except Exception:
        logger.exception("Failed to query parentless topics")
        raise


def get_orphaned_chunk_ids(conn: "lb.Connection") -> list[str]:
    """Chunk anchors with no CONTAINS edge at all - a completeness gap,
    not a constraint the engine itself can catch (multiplicity constraints
    only prevent a *second* edge, not a *missing* one). Used by the
    staged-build validation step before an atomic swap; see staged_build.py."""
    try:
        result = conn.execute(
            "MATCH (c:Chunk) WHERE NOT EXISTS { MATCH ()-[:CONTAINS]->(c) } RETURN c.id"
        )
        return [row[0] for row in result]
    except Exception:
        logger.exception("Failed to query orphaned chunks")
        raise
