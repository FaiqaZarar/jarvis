"""Tests for the LadybugDB-backed topic hierarchy: constraint enforcement,
core operations, the SQLite cache sync, and the staged-build atomic swap."""

import json
import sqlite3
from pathlib import Path

import pytest

from jarvis_server.internal import db
from jarvis_server.internal.graph_db import graph_db, staged_build, sync
from jarvis_server.internal.migrate import MIGRATIONS_DIR, run_migrations


@pytest.fixture
def sqlite_conn(tmp_path: Path):
    db_path = tmp_path / "test.db"
    run_migrations(db_path)
    connection = db.get_connection(db_path)
    yield connection
    connection.close()


@pytest.fixture
def graph_conn(tmp_path: Path):
    graph_path = tmp_path / "test_graph.lbdb"
    return graph_db.get_connection(graph_path)


def test_single_parent_enforced_by_engine(graph_conn):
    graph_db.insert_topic(graph_conn, "root", "Root")
    graph_db.insert_topic(graph_conn, "parent-a", "A")
    graph_db.insert_topic(graph_conn, "parent-b", "B")
    graph_db.insert_topic(graph_conn, "child", "Child")

    graph_conn.execute(
        "MATCH (c:Topic {id:'child'}), (p:Topic {id:'parent-a'}) "
        "CREATE (c)-[:PARENT_OF]->(p)"
    )
    with pytest.raises(Exception):
        graph_conn.execute(
            "MATCH (c:Topic {id:'child'}), (p:Topic {id:'parent-b'}) "
            "CREATE (c)-[:PARENT_OF]->(p)"
        )


def test_single_membership_enforced_by_engine(graph_conn):
    graph_db.insert_topic(graph_conn, "topic-a", "A")
    graph_db.insert_topic(graph_conn, "topic-b", "B")
    graph_db.insert_chunk_anchor(graph_conn, "chunk-1")

    graph_conn.execute(
        "MATCH (t:Topic {id:'topic-a'}), (c:Chunk {id:'chunk-1'}) "
        "CREATE (t)-[:CONTAINS {confidence: 0.9}]->(c)"
    )
    with pytest.raises(Exception):
        graph_conn.execute(
            "MATCH (t:Topic {id:'topic-b'}), (c:Chunk {id:'chunk-1'}) "
            "CREATE (t)-[:CONTAINS {confidence: 0.5}]->(c)"
        )


def test_one_topic_can_contain_many_chunks(graph_conn):
    graph_db.insert_topic(graph_conn, "topic-a", "A")
    graph_db.insert_chunk_anchor(graph_conn, "chunk-1")
    graph_db.insert_chunk_anchor(graph_conn, "chunk-2")
    graph_db.place_chunk(graph_conn, "chunk-1", "topic-a", 0.9)
    graph_db.place_chunk(graph_conn, "chunk-2", "topic-a", 0.8)
    assert len(graph_db.get_direct_chunks(graph_conn, "topic-a")) == 2


def test_reparenting_is_not_a_constraint_violation(graph_conn):
    graph_db.insert_topic(graph_conn, "root", "Root")
    graph_db.insert_topic(graph_conn, "new-parent", "New Parent")
    graph_db.insert_topic(graph_conn, "child", "Child")
    graph_db.set_parent(graph_conn, "child", "root")
    assert graph_db.get_children(graph_conn, "root") == ["child"]
    graph_db.set_parent(graph_conn, "child", "new-parent")
    assert graph_db.get_children(graph_conn, "root") == []
    assert graph_db.get_children(graph_conn, "new-parent") == ["child"]


def test_rechunking_placement_is_not_a_constraint_violation(graph_conn):
    graph_db.insert_topic(graph_conn, "topic-a", "A")
    graph_db.insert_topic(graph_conn, "topic-b", "B")
    graph_db.insert_chunk_anchor(graph_conn, "chunk-1")
    graph_db.place_chunk(graph_conn, "chunk-1", "topic-a", 0.9)
    assert graph_db.get_chunk_topic(graph_conn, "chunk-1")[0] == "topic-a"
    graph_db.place_chunk(graph_conn, "chunk-1", "topic-b", 0.7)
    assert graph_db.get_chunk_topic(graph_conn, "chunk-1")[0] == "topic-b"


def test_ancestor_path_multi_level(graph_conn):
    graph_db.insert_topic(graph_conn, "root", "Root")
    graph_db.insert_topic(graph_conn, "mid", "Mid")
    graph_db.insert_topic(graph_conn, "leaf", "Leaf")
    graph_db.set_parent(graph_conn, "mid", "root")
    graph_db.set_parent(graph_conn, "leaf", "mid")
    assert graph_db.get_ancestor_path(graph_conn, "leaf") == ["root", "mid", "leaf"]


def test_ancestor_path_four_levels_is_correctly_ordered(graph_conn):
    """3 levels alone doesn't prove the ordering is a real guarantee
    rather than an accident of engine internals - go one level deeper so
    the explicit ORDER BY on hop count actually has something to prove."""
    graph_db.insert_topic(graph_conn, "root", "Root")
    graph_db.insert_topic(graph_conn, "a", "A")
    graph_db.insert_topic(graph_conn, "b", "B")
    graph_db.insert_topic(graph_conn, "leaf", "Leaf")
    graph_db.set_parent(graph_conn, "a", "root")
    graph_db.set_parent(graph_conn, "b", "a")
    graph_db.set_parent(graph_conn, "leaf", "b")
    assert graph_db.get_ancestor_path(graph_conn, "leaf") == ["root", "a", "b", "leaf"]


def test_get_chunk_topic_returns_none_when_unplaced(graph_conn):
    graph_db.insert_chunk_anchor(graph_conn, "orphan-chunk")
    assert graph_db.get_chunk_topic(graph_conn, "orphan-chunk") is None


def test_direct_and_subtree_chunk_counts(graph_conn):
    graph_db.insert_topic(graph_conn, "root", "Root")
    graph_db.insert_topic(graph_conn, "mid", "Mid")
    graph_db.insert_topic(graph_conn, "leaf", "Leaf")
    graph_db.set_parent(graph_conn, "mid", "root")
    graph_db.set_parent(graph_conn, "leaf", "mid")
    for topic_id in ("root", "mid", "leaf"):
        chunk_id = f"chunk-{topic_id}"
        graph_db.insert_chunk_anchor(graph_conn, chunk_id)
        graph_db.place_chunk(graph_conn, chunk_id, topic_id, 0.9)
    assert graph_db.get_direct_chunk_count(graph_conn, "root") == 1
    assert graph_db.get_subtree_chunk_count(graph_conn, "root") == 3
    assert graph_db.get_direct_chunk_count(graph_conn, "leaf") == 1
    assert graph_db.get_subtree_chunk_count(graph_conn, "leaf") == 1


def test_orphan_detection(graph_conn):
    graph_db.insert_topic(graph_conn, "topic-a", "A")
    graph_db.insert_chunk_anchor(graph_conn, "placed-chunk")
    graph_db.insert_chunk_anchor(graph_conn, "orphan-chunk")
    graph_db.place_chunk(graph_conn, "placed-chunk", "topic-a", 0.9)
    assert graph_db.get_orphaned_chunk_ids(graph_conn) == ["orphan-chunk"]


def test_sync_populates_children_ancestor_path_and_counts(sqlite_conn, graph_conn):
    sqlite_conn.execute("INSERT INTO topics (id, name) VALUES ('root', 'Root')")
    sqlite_conn.execute("INSERT INTO topics (id, name) VALUES ('child', 'Child')")
    sqlite_conn.commit()
    graph_db.insert_topic(graph_conn, "root", "Root")
    graph_db.insert_topic(graph_conn, "child", "Child")
    graph_db.set_parent(graph_conn, "child", "root")
    graph_db.insert_chunk_anchor(graph_conn, "chunk-1")
    graph_db.place_chunk(graph_conn, "chunk-1", "child", 0.9)

    stats = sync.sync_all(sqlite_conn, graph_conn)
    assert stats["topics_synced"] == 2

    root_row = sqlite_conn.execute(
        "SELECT children_ids, subtree_chunk_count FROM topics WHERE id = 'root'"
    ).fetchone()
    assert json.loads(root_row["children_ids"]) == ["child"]
    assert root_row["subtree_chunk_count"] == 1

    child_row = sqlite_conn.execute(
        "SELECT ancestor_path, direct_chunk_count FROM topics WHERE id = 'child'"
    ).fetchone()
    assert json.loads(child_row["ancestor_path"]) == ["root", "child"]
    assert child_row["direct_chunk_count"] == 1


def test_sync_populates_chunk_topic_cache(sqlite_conn, graph_conn):
    sqlite_conn.execute(
        "INSERT INTO documents (content_hash, normalized_markdown, first_seen_at, last_ingested_at) "
        "VALUES ('doc1', '# Test', '2026-01-01', '2026-01-01')"
    )
    sqlite_conn.execute(
        "INSERT INTO chunks (chunk_id, content_hash, ordinal, chunk_hash) "
        "VALUES ('chunk-1', 'doc1', 0, 'chunk-1')"
    )
    sqlite_conn.execute("INSERT INTO topics (id, name) VALUES ('topic-a', 'A')")
    sqlite_conn.commit()
    graph_db.insert_topic(graph_conn, "topic-a", "A")
    graph_db.insert_chunk_anchor(graph_conn, "chunk-1")
    graph_db.place_chunk(graph_conn, "chunk-1", "topic-a", 0.85)

    stats = sync.sync_all(sqlite_conn, graph_conn)
    assert stats["chunks_synced"] == 1
    row = sqlite_conn.execute(
        "SELECT topic_id, topic_confidence FROM chunks WHERE chunk_id = 'chunk-1'"
    ).fetchone()
    assert row["topic_id"] == "topic-a"
    assert row["topic_confidence"] == 0.85


def test_migration_drops_old_tables_cleanly(sqlite_conn):
    tables = sqlite_conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()
    table_names = {row["name"] for row in tables}
    assert "parent_of" not in table_names
    assert "contains" not in table_names


def test_staged_build_success_swaps_and_syncs(sqlite_conn, tmp_path):
    sqlite_conn.execute(
        "INSERT INTO documents (content_hash, normalized_markdown, first_seen_at, last_ingested_at) "
        "VALUES ('doc1', '# Test', '2026-01-01', '2026-01-01')"
    )
    sqlite_conn.execute(
        "INSERT INTO chunks (chunk_id, content_hash, ordinal, chunk_hash) "
        "VALUES ('chunk-1', 'doc1', 0, 'chunk-1')"
    )
    sqlite_conn.execute("INSERT INTO topics (id, name) VALUES ('topic-a', 'A')")
    sqlite_conn.commit()
    live_path = tmp_path / "live_graph.lbdb"

    def populate(conn):
        graph_db.insert_topic(conn, "topic-a", "A", reserved=True)
        graph_db.insert_chunk_anchor(conn, "chunk-1")
        graph_db.place_chunk(conn, "chunk-1", "topic-a", 0.9)

    stats = staged_build.build_and_swap(sqlite_conn, live_path, populate)
    assert live_path.exists()
    assert stats["chunks_synced"] == 1
    row = sqlite_conn.execute(
        "SELECT topic_id FROM chunks WHERE chunk_id = 'chunk-1'"
    ).fetchone()
    assert row["topic_id"] == "topic-a"


def test_staged_build_failure_leaves_live_graph_untouched(sqlite_conn, tmp_path):
    sqlite_conn.execute(
        "INSERT INTO documents (content_hash, normalized_markdown, first_seen_at, last_ingested_at) "
        "VALUES ('doc1', '# Test', '2026-01-01', '2026-01-01')"
    )
    sqlite_conn.execute(
        "INSERT INTO chunks (chunk_id, content_hash, ordinal, chunk_hash) "
        "VALUES ('chunk-1', 'doc1', 0, 'chunk-1')"
    )
    sqlite_conn.execute("INSERT INTO topics (id, name) VALUES ('topic-a', 'A')")
    sqlite_conn.commit()
    live_path = tmp_path / "live_graph.lbdb"

    def good_populate(conn):
        graph_db.insert_topic(conn, "topic-a", "A", reserved=True)
        graph_db.insert_chunk_anchor(conn, "chunk-1")
        graph_db.place_chunk(conn, "chunk-1", "topic-a", 0.9)

    staged_build.build_and_swap(sqlite_conn, live_path, good_populate)
    live_mtime_before = live_path.stat().st_mtime_ns

    def bad_populate(conn):
        graph_db.insert_topic(conn, "topic-a", "A", reserved=True)
        graph_db.insert_chunk_anchor(conn, "chunk-1")

    with pytest.raises(staged_build.ValidationError):
        staged_build.build_and_swap(sqlite_conn, live_path, bad_populate)

    assert live_path.stat().st_mtime_ns == live_mtime_before
    row = sqlite_conn.execute(
        "SELECT topic_id FROM chunks WHERE chunk_id = 'chunk-1'"
    ).fetchone()
    assert row["topic_id"] == "topic-a"


def test_staged_build_failure_catches_missing_anchor_node(sqlite_conn, tmp_path):
    sqlite_conn.execute(
        "INSERT INTO documents (content_hash, normalized_markdown, first_seen_at, last_ingested_at) "
        "VALUES ('doc1', '# Test', '2026-01-01', '2026-01-01')"
    )
    sqlite_conn.execute(
        "INSERT INTO chunks (chunk_id, content_hash, ordinal, chunk_hash) "
        "VALUES ('chunk-1', 'doc1', 0, 'chunk-1')"
    )
    sqlite_conn.commit()
    live_path = tmp_path / "live_graph.lbdb"

    def populate_missing_anchor(conn):
        pass

    with pytest.raises(staged_build.ValidationError):
        staged_build.build_and_swap(sqlite_conn, live_path, populate_missing_anchor)


def test_migration_converges_correctly_from_a_pre_existing_database(tmp_path):
    """The exact scenario that broke before: a database that already
    applied the original 0001 (parent_of/contains present, no cache
    columns) must still converge correctly when the full migration set
    runs against it - not silently skip 0001 and then crash later on a
    missing column."""
    import shutil

    import jarvis_server.internal.migrate as migrate_module

    old_only_dir = tmp_path / "migrations_0001_only"
    old_only_dir.mkdir()
    shutil.copy(MIGRATIONS_DIR / "0001_initial_schema.sql", old_only_dir)

    db_path = tmp_path / "old_style.db"

    original_dir = migrate_module.MIGRATIONS_DIR
    migrate_module.MIGRATIONS_DIR = old_only_dir
    try:
        run_migrations(db_path)
    finally:
        migrate_module.MIGRATIONS_DIR = original_dir

    conn = sqlite3.connect(db_path)
    tables = {row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}
    assert "parent_of" in tables  # confirms this really is the old shape

    # Now run the REAL full migration set against this same pre-existing db.
    run_migrations(db_path)

    tables = {row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}
    assert "parent_of" not in tables
    assert "contains" not in tables

    topic_cols = [row[1] for row in conn.execute("PRAGMA table_info(topics)").fetchall()]
    assert "children_ids" in topic_cols

    chunk_cols = [row[1] for row in conn.execute("PRAGMA table_info(chunks)").fetchall()]
    assert "topic_id" in chunk_cols


def test_set_parent_raises_for_nonexistent_parent(graph_conn):
    graph_db.insert_topic(graph_conn, "child", "Child")
    with pytest.raises(ValueError):
        graph_db.set_parent(graph_conn, "child", "does-not-exist")


def test_set_parent_raises_for_nonexistent_child(graph_conn):
    graph_db.insert_topic(graph_conn, "parent", "Parent")
    with pytest.raises(ValueError):
        graph_db.set_parent(graph_conn, "does-not-exist", "parent")


def test_place_chunk_raises_for_nonexistent_topic(graph_conn):
    graph_db.insert_chunk_anchor(graph_conn, "chunk-1")
    with pytest.raises(ValueError):
        graph_db.place_chunk(graph_conn, "chunk-1", "does-not-exist", 0.9)


def test_place_chunk_raises_for_nonexistent_chunk(graph_conn):
    graph_db.insert_topic(graph_conn, "topic-a", "A")
    with pytest.raises(ValueError):
        graph_db.place_chunk(graph_conn, "does-not-exist", "topic-a", 0.9)


def test_staged_build_rejects_a_parentless_non_root_topic(sqlite_conn, tmp_path):
    sqlite_conn.execute("INSERT INTO topics (id, name) VALUES ('topic-a', 'A')")
    sqlite_conn.commit()
    live_path = tmp_path / "live_graph.lbdb"

    def populate_parentless(conn):
        # A real root would be reserved=True; this leaves a non-root
        # topic with no PARENT_OF edge at all.
        graph_db.insert_topic(conn, "topic-a", "A", reserved=False)

    with pytest.raises(staged_build.ValidationError):
        staged_build.build_and_swap(sqlite_conn, live_path, populate_parentless)


def test_sync_clears_stale_topic_cache_when_placement_is_removed(sqlite_conn, graph_conn):
    sqlite_conn.execute(
        "INSERT INTO documents (content_hash, normalized_markdown, first_seen_at, last_ingested_at) "
        "VALUES ('doc1', '# Test', '2026-01-01', '2026-01-01')"
    )
    sqlite_conn.execute(
        "INSERT INTO chunks (chunk_id, content_hash, ordinal, chunk_hash) "
        "VALUES ('chunk-1', 'doc1', 0, 'chunk-1')"
    )
    sqlite_conn.execute("INSERT INTO topics (id, name) VALUES ('topic-a', 'A')")
    sqlite_conn.commit()
    graph_db.insert_topic(graph_conn, "topic-a", "A")
    graph_db.insert_chunk_anchor(graph_conn, "chunk-1")
    graph_db.place_chunk(graph_conn, "chunk-1", "topic-a", 0.9)

    sync.sync_all(sqlite_conn, graph_conn)
    row = sqlite_conn.execute(
        "SELECT topic_id, topic_confidence FROM chunks WHERE chunk_id = 'chunk-1'"
    ).fetchone()
    assert row["topic_id"] == "topic-a"

    # Placement removed from the graph - a re-sync must clear the cache,
    # not leave the stale topic-a values in place.
    graph_conn.execute("MATCH ()-[e:CONTAINS]->(c:Chunk {id: 'chunk-1'}) DELETE e")
    sync.sync_all(sqlite_conn, graph_conn)

    row = sqlite_conn.execute(
        "SELECT topic_id, topic_confidence FROM chunks WHERE chunk_id = 'chunk-1'"
    ).fetchone()
    assert row["topic_id"] is None
    assert row["topic_confidence"] is None


def test_stale_staging_file_logs_a_warning_before_being_removed(sqlite_conn, tmp_path, caplog):
    sqlite_conn.execute("INSERT INTO topics (id, name) VALUES ('topic-a', 'A')")
    sqlite_conn.commit()
    live_path = tmp_path / "live_graph.lbdb"
    staging_path = live_path.with_suffix(live_path.suffix + ".staging")
    staging_path.write_bytes(b"leftover from a previous failed build")

    def populate(conn):
        graph_db.insert_topic(conn, "topic-a", "A", reserved=True)

    with caplog.at_level("WARNING"):
        staged_build.build_and_swap(sqlite_conn, live_path, populate)

    assert any("leftover staging file" in record.message for record in caplog.records)


def test_get_connection_does_not_lock_out_a_retry_after_schema_init_failure(tmp_path, monkeypatch):
    graph_path = tmp_path / "retry_test.lbdb"

    broken_statements = graph_db._SCHEMA_STATEMENTS + ["THIS IS NOT VALID CYPHER"]
    monkeypatch.setattr(graph_db, "_SCHEMA_STATEMENTS", broken_statements)
    with pytest.raises(Exception):
        graph_db.get_connection(graph_path)

    monkeypatch.undo()

    # If the failed attempt above leaked its Database handle, this second,
    # correctly-configured open on the same path would fail too.
    conn = graph_db.get_connection(graph_path)
    graph_db.close_connection(conn)
