"""topics table."""

import sqlite3
from typing import Optional

from jarvis_server.internal.db._middleware import read_query, write_query


@write_query
def insert_topic(
    conn: sqlite3.Connection,
    topic_id: str,
    name: str,
    description: Optional[str] = None,
    centroid: Optional[bytes] = None,
    level: int = 0,
    reserved: bool = False,
) -> None:
    conn.execute(
        """
        INSERT INTO topics (id, name, description, centroid, level, reserved)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (topic_id, name, description, centroid, level, int(reserved)),
    )


@read_query
def get_topic(conn: sqlite3.Connection, topic_id: str) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM topics WHERE id = ?", (topic_id,)).fetchone()


@read_query
def get_live_topics(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Every non-tombstoned, non-reserved topic. This is the prior-state
    input reconciliation (J-09) matches a fresh clustering against - the
    reserved `Unfiled` node and already-tombstoned nodes are never
    match candidates (spec §6)."""
    return conn.execute(
        "SELECT * FROM topics WHERE tombstoned = 0 AND reserved = 0 ORDER BY id"
    ).fetchall()


@write_query
def update_topic(
    conn: sqlite3.Connection,
    topic_id: str,
    *,
    name: str,
    description: Optional[str],
    centroid: Optional[bytes],
    level: int,
    version: int,
) -> None:
    """Overwrite a carried-forward topic's mutable fields after a
    re-sync (J-09). Identity (id) and lifecycle flags (reserved,
    tombstoned, superseded_by) are not touched here."""
    conn.execute(
        """
        UPDATE topics
        SET name = ?, description = ?, centroid = ?, level = ?, version = ?
        WHERE id = ?
        """,
        (name, description, centroid, level, version, topic_id),
    )


@write_query
def mark_topic_tombstoned(
    conn: sqlite3.Connection, topic_id: str, superseded_by: Optional[str] = None
) -> None:
    """Tombstone a topic that reconciliation matched to nothing this sync
    (spec §6: never hard-delete). `superseded_by` is a comma-separated
    list of topic ids for the merge case; None for a plain tombstone."""
    conn.execute(
        "UPDATE topics SET tombstoned = 1, superseded_by = ? WHERE id = ?",
        (superseded_by, topic_id),
    )


@read_query
def get_all_topic_ids(conn: sqlite3.Connection) -> set[str]:
    """Every topic id, regardless of tombstoned/reserved status - unlike
    get_live_topics, this is an identity-only existence check (e.g.
    "does this id already have a row"), not a match-candidate set."""
    return {row["id"] for row in conn.execute("SELECT id FROM topics").fetchall()}


@read_query
def count_live_topics(conn: sqlite3.Connection) -> int:
    """Same filter as get_live_topics, but a count - for callers that only
    need to know "are there any", not the rows themselves."""
    return conn.execute(
        "SELECT COUNT(*) FROM topics WHERE reserved = 0 AND tombstoned = 0"
    ).fetchone()[0]


@write_query
def update_topic_cache(
    conn: sqlite3.Connection,
    topic_id: str,
    children_ids: str,
    ancestor_path: str,
    direct_chunk_count: int,
    subtree_chunk_count: int,
) -> None:
    """Overwrite a topic's graph-derived cache columns. Written only by
    graph_db/sync.py, right after a staged-build swap - LadybugDB is the
    source of truth for this data; these columns exist purely so reads
    don't need a graph round-trip (see sync.py's module docstring)."""
    conn.execute(
        """
        UPDATE topics
        SET children_ids = ?, ancestor_path = ?,
            direct_chunk_count = ?, subtree_chunk_count = ?
        WHERE id = ?
        """,
        (children_ids, ancestor_path, direct_chunk_count, subtree_chunk_count, topic_id),
    )


@read_query
def get_ordered_topics_by_id(conn: sqlite3.Connection, ids: list[str]) -> list[sqlite3.Row]:
    """The given topic ids, ordered by (name, id) for a stable response -
    e.g. hierarchy.py's children listing, which resolves ids out of a
    topic's cached children_ids JSON in whatever order that array holds
    them."""
    if not ids:
        return []
    placeholders = ",".join("?" for _ in ids)
    return conn.execute(
        f"SELECT id FROM topics WHERE id IN ({placeholders}) ORDER BY name, id", ids
    ).fetchall()


@read_query
def get_topic_ids_by_name(conn: sqlite3.Connection, name: str) -> list[str]:
    """Exact-match topic ids for a name - ambiguous by construction (names
    aren't unique), used as a placeholder topic-filter resolution until
    real breadcrumb-path resolution exists (see search.py's
    _resolve_topic_filter_ids)."""
    rows = conn.execute("SELECT id FROM topics WHERE name = ?", (name,)).fetchall()
    return [row["id"] for row in rows]


@read_query
def get_related_topic_candidates(conn: sqlite3.Connection, exclude_topic_id: str) -> list[sqlite3.Row]:
    """Every live, non-reserved topic except the given one - related()'s
    candidate pool for its centroid-cosine KNN (J10_DESIGN.md §2.5)."""
    return conn.execute(
        "SELECT id, centroid, ancestor_path FROM topics "
        "WHERE tombstoned = 0 AND reserved = 0 AND id != ?",
        (exclude_topic_id,),
    ).fetchall()
