"""Sync LadybugDB's current state into SQLite's JSON/scalar cache columns.

LadybugDB is the source of truth. This module is the only thing that
writes to topics.children_ids / ancestor_path / direct_chunk_count /
subtree_chunk_count / chunks.topic_id / chunks.topic_confidence in
SQLite - those columns exist purely for fast, easy reads, and should
never be trusted over the graph if the two ever disagree.

This should be called exactly once, after a successful staged-build swap
(see staged_build.build_and_swap) - never automatically per individual
graph write, and never before the graph's own completeness validation has
passed. Calling it at any other time risks the SQLite cache reflecting a
mix of old and new hierarchy state, which spec §5.3 already rules out for
the graph itself.
"""

import json
import sqlite3

from jarvis_server.internal import db
from jarvis_server.internal.graph_db import graph_db
from jarvis_server.internal.logging.logging_config import get_logger

logger = get_logger(__name__)


def sync_all(sqlite_conn: sqlite3.Connection, graph_conn: "graph_db.lb.Connection") -> dict:
    """Rebuild every cached column in SQLite from the graph's current state.

    Returns a stats dict: {topics_synced, chunks_synced}.
    """
    stats = {"topics_synced": 0, "chunks_synced": 0}
    try:
        _sync_topics(sqlite_conn, graph_conn, stats)
        _sync_chunks(sqlite_conn, graph_conn, stats)
        sqlite_conn.commit()
        return stats
    except Exception:
        sqlite_conn.rollback()
        logger.exception("Failed to sync graph state into SQLite cache")
        raise


def _sync_topics(sqlite_conn: sqlite3.Connection, graph_conn, stats: dict) -> None:
    for topic_id in db.get_all_topic_ids(sqlite_conn):
        children = graph_db.get_children(graph_conn, topic_id)
        ancestor_path = graph_db.get_ancestor_path(graph_conn, topic_id)
        direct_count = graph_db.get_direct_chunk_count(graph_conn, topic_id)
        subtree_count = graph_db.get_subtree_chunk_count(graph_conn, topic_id)

        db.update_topic_cache(
            sqlite_conn, topic_id,
            json.dumps(children), json.dumps(ancestor_path),
            direct_count, subtree_count,
            commit=False,
        )
        stats["topics_synced"] += 1


def _sync_chunks(sqlite_conn: sqlite3.Connection, graph_conn, stats: dict) -> None:
    for chunk_id in db.get_all_chunk_ids(sqlite_conn):
        placement = graph_db.get_chunk_topic(graph_conn, chunk_id)
        if placement is not None:
            topic_id, confidence = placement
            db.update_chunk_placement(sqlite_conn, chunk_id, topic_id, confidence, commit=False)
            stats["chunks_synced"] += 1
        else:
            # No current graph placement - clear any stale cached values
            # from a previous sync rather than leaving them in place, per
            # this module's own invariant that the cache must never be
            # trusted over the graph if the two disagree.
            db.update_chunk_placement(sqlite_conn, chunk_id, None, None, commit=False)
