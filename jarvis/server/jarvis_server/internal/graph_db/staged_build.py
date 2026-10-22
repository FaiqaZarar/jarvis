"""Staged build + validate + atomic swap for the topic hierarchy graph.

Sync timing: the SQLite cache sync runs exactly once, after a successful
swap - never per-write, never before validation passes. Per-write syncing
would let a query see a mix of old and new hierarchy state mid-build,
which spec §5.3 already rules out for the graph itself; this preserves
that same guarantee for the cache too.

Failure handling: if validation fails, the swap simply does not happen,
and therefore neither does the sync. The old graph file and the old
SQLite cache are left exactly as they were - the same "a failed pull
leaves prior state fully intact" principle spec §5.3 already requires
elsewhere, no special-case code needed.

What "validation" does and does not need to check:
  - Single-parent / single-membership: NOT re-checked here. LadybugDB's
    multiplicity constraints are enforced continuously *during* the
    build - a violating write is rejected the instant it happens - so
    by the time the build finishes without error, these two invariants
    already hold.
  - Completeness: IS checked here. The engine has no way to know if the
    organize engine simply forgot to place a chunk anywhere, or forgot to
    parent a topic - that's a real gap the multiplicity constraints can't
    catch (MANY_ONE/ONE_MANY only prevent a *second* edge, never a
    *missing* one), so both are checked explicitly before the swap
    proceeds: every chunk must have a placement, and every non-root topic
    must have a parent.

IMPORTANT: writes must be flushed to disk before the atomic rename - an
unclosed connection's writes can be invisible to a fresh connection
opened after the underlying file is renamed out from under it. Always
close the staging connection (via graph_db.close_connection, not
conn.close() alone) before os.replace.
"""

import os
import sqlite3
from pathlib import Path

from jarvis_server.internal import db
from jarvis_server.internal.graph_db import graph_db, sync
from jarvis_server.internal.logging.logging_config import get_logger

logger = get_logger(__name__)


class ValidationError(Exception):
    """Raised when a staged graph build fails its pre-swap completeness
    check. The staged file is left in place for inspection; the live
    graph and SQLite cache are untouched."""


def build_and_swap(
    sqlite_conn: sqlite3.Connection,
    live_graph_path: Path,
    populate_fn,
) -> dict:
    """Build a new graph into a staging file, validate it, and only then
    atomically swap it in for the live file.

    Args:
        sqlite_conn: open SQLite connection, used for the post-swap sync
            and for the completeness check.
        live_graph_path: path to the graph file currently in use.
        populate_fn: a callable that receives a fresh graph connection
            (pointed at the staging file) and does all the actual
            clustering/reconciliation writes. Supplied by the organize
            engine.

    Returns a stats dict: {topics_synced, chunks_synced}.

    Raises ValidationError if the completeness check fails; in that case
    nothing about the live graph or the SQLite cache has changed.
    """
    staging_path = live_graph_path.with_suffix(live_graph_path.suffix + ".staging")

    if staging_path.exists():
        logger.warning(
            "Removing leftover staging file at %s before starting a new build - "
            "this was left behind by a previous failed build; its evidence is "
            "being discarded now.", staging_path,
        )
        staging_path.unlink()

    staging_conn = None
    try:
        staging_conn = graph_db.get_connection(staging_path)
        populate_fn(staging_conn)

        _validate_completeness(sqlite_conn, staging_conn)

        graph_db.close_connection(staging_conn)
        staging_conn = None

        os.replace(staging_path, live_graph_path)
        logger.info("Graph build validated and swapped in at %s", live_graph_path)

    except ValidationError:
        logger.warning(
            "Staged graph build failed completeness validation; "
            "live graph and SQLite cache left untouched. Staged file "
            "kept at %s for inspection.", staging_path,
        )
        if staging_conn is not None:
            graph_db.close_connection(staging_conn)
        raise
    except Exception:
        logger.exception("Unexpected failure during staged graph build")
        if staging_conn is not None:
            graph_db.close_connection(staging_conn)
        if staging_path.exists():
            staging_path.unlink()
        raise

    live_conn = graph_db.get_connection(live_graph_path)
    try:
        return sync.sync_all(sqlite_conn, live_conn)
    finally:
        graph_db.close_connection(live_conn)


def _validate_completeness(sqlite_conn: sqlite3.Connection, staging_conn) -> None:
    """Every chunk known to SQLite must have a placement in the staged
    graph, and every non-root topic must have a parent."""
    parentless = graph_db.get_parentless_topic_ids(staging_conn)
    if parentless:
        raise ValidationError(
            f"{len(parentless)} non-root topic(s) have no parent in the staged graph: "
            f"{parentless[:10]}{'...' if len(parentless) > 10 else ''}"
        )

    orphans = graph_db.get_orphaned_chunk_ids(staging_conn)
    if orphans:
        raise ValidationError(
            f"{len(orphans)} chunk(s) have no topic placement in the staged graph: "
            f"{orphans[:10]}{'...' if len(orphans) > 10 else ''}"
        )

    sqlite_chunk_ids = db.get_all_chunk_ids(sqlite_conn)
    graph_chunk_ids = set(graph_db.get_all_chunk_ids(staging_conn))
    missing = sqlite_chunk_ids - graph_chunk_ids
    if missing:
        raise ValidationError(
            f"{len(missing)} chunk(s) known to SQLite have no anchor node in the "
            f"staged graph at all: {list(missing)[:10]}{'...' if len(missing) > 10 else ''}"
        )
