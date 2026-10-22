"""J-02 acceptance check: every table can be inserted into and queried."""

import sqlite3
from pathlib import Path

import pytest

from jarvis_server.internal import db
from jarvis_server.internal.migrate import run_migrations


@pytest.fixture
def conn(tmp_path: Path):
    db_path = tmp_path / "test.db"
    run_migrations(db_path)
    connection = db.get_connection(db_path)
    yield connection
    connection.close()


def test_documents_insert_and_query(conn: sqlite3.Connection):
    db.insert_document(conn, "hash1", "# Hello", "2026-01-01T00:00:00", "2026-01-01T00:00:00")
    row = db.get_document(conn, "hash1")
    assert row["content_hash"] == "hash1"
    assert row["normalized_markdown"] == "# Hello"


def test_document_sources_insert_and_query(conn: sqlite3.Connection):
    db.insert_document(conn, "hash1", "# Hello", "2026-01-01T00:00:00", "2026-01-01T00:00:00")
    db.insert_document_source(
        conn,
        source_id="/tmp/file.md",
        connector="filesystem",
        content_hash="hash1",
        source_last_seen="2026-01-01T00:00:00",
    )
    rows = db.get_document_sources_for_document(conn, "hash1")
    assert len(rows) == 1
    assert rows[0]["connector"] == "filesystem"


def test_chunks_insert_and_query(conn: sqlite3.Connection):
    db.insert_document(conn, "hash1", "# Hello", "2026-01-01T00:00:00", "2026-01-01T00:00:00")
    db.insert_chunk(conn, "chunk1", "hash1", 0, "chunkhash1", heading_path="Intro")
    rows = db.get_chunks_for_document(conn, "hash1")
    assert len(rows) == 1
    assert rows[0]["heading_path"] == "Intro"


def test_topics_insert_and_query(conn: sqlite3.Connection):
    """Topic/chunk relationships (PARENT_OF, CONTAINS) now live in
    LadybugDB, not SQLite - see tests/test_graph_db.py. This just checks
    the topics table itself, and its synced-cache columns, still work."""
    db.insert_topic(conn, "topic-root", "Root", reserved=True)
    root = db.get_topic(conn, "topic-root")
    assert root["reserved"] == 1
    assert root["children_ids"] is None  # unset until the graph sync runs


def test_topic_events_insert_and_query(conn: sqlite3.Connection):
    db.insert_topic(conn, "topic-1", "Some Topic")
    db.insert_topic_event(
        conn, "topic-1", sync_id="sync-1", event_type="born", created_at="2026-01-01T00:00:00"
    )
    events = db.get_events_for_topic(conn, "topic-1")
    assert len(events) == 1
    assert events[0]["event_type"] == "born"


# Single-parent / single-membership constraint tests now live in
# tests/test_graph_db.py, enforced by LadybugDB itself rather than a
# SQLite PRIMARY KEY - see test_single_parent_enforced_by_engine and
# test_single_membership_enforced_by_engine there.


# ---------------------------------------------------------------------
# J-04: filesystem-connector fingerprint columns + their db.py functions
# ---------------------------------------------------------------------

def test_document_source_fingerprint_columns_round_trip(conn: sqlite3.Connection):
    db.insert_document(conn, "hash1", "# Hello", "2026-01-01T00:00:00", "2026-01-01T00:00:00")
    db.insert_document_source(
        conn,
        source_id="/watched/file.md",
        connector="filesystem",
        content_hash="hash1",
        source_last_seen="2026-01-01T00:00:00",
        source_mtime=1234.5,
        source_size=42,
        source_inode="1:5678",
    )
    rows = db.get_active_document_sources(conn, "filesystem")
    assert len(rows) == 1
    assert rows[0]["source_mtime"] == 1234.5
    assert rows[0]["source_size"] == 42
    assert rows[0]["source_inode"] == "1:5678"


def test_get_active_document_sources_excludes_tombstoned(conn: sqlite3.Connection):
    db.insert_document(conn, "hash1", "# Hello", "2026-01-01T00:00:00", "2026-01-01T00:00:00")
    db.insert_document_source(
        conn, source_id="/a.md", connector="filesystem", content_hash="hash1",
        source_last_seen="2026-01-01T00:00:00",
    )
    db.tombstone_document_source(conn, "filesystem", "/a.md")
    rows = db.get_active_document_sources(conn, "filesystem")
    assert len(rows) == 0


def test_touch_document_source_seen_refreshes_timestamp_only(conn: sqlite3.Connection):
    db.insert_document(conn, "hash1", "# Hello", "2026-01-01T00:00:00", "2026-01-01T00:00:00")
    db.insert_document_source(
        conn, source_id="/a.md", connector="filesystem", content_hash="hash1",
        source_last_seen="2026-01-01T00:00:00", source_mtime=100.0, source_size=10,
    )
    db.touch_document_source_seen(conn, "filesystem", "/a.md", "2026-01-02T00:00:00")
    rows = db.get_active_document_sources(conn, "filesystem")
    assert rows[0]["source_mtime"] == 100.0  # unchanged
    sources = db.get_document_sources_for_document(conn, "hash1")
    assert sources[0]["source_last_seen"] == "2026-01-02T00:00:00"


def test_rename_document_source_repoints_in_place(conn: sqlite3.Connection):
    db.insert_document(conn, "hash1", "# Hello", "2026-01-01T00:00:00", "2026-01-01T00:00:00")
    db.insert_document_source(
        conn, source_id="/old_name.md", connector="filesystem", content_hash="hash1",
        source_last_seen="2026-01-01T00:00:00", source_inode="1:9999",
    )
    db.rename_document_source(
        conn,
        connector="filesystem",
        old_source_id="/old_name.md",
        new_source_id="/new_name.md",
        content_hash="hash1",
        source_last_seen="2026-01-02T00:00:00",
        source_modified_at="2026-01-02T00:00:00",
        source_mtime=200.0,
        source_size=20,
        source_inode="1:9999",
    )
    sources = db.get_document_sources_for_document(conn, "hash1")
    assert len(sources) == 1  # repointed, not a second row
    assert sources[0]["source_id"] == "/new_name.md"
    assert sources[0]["tombstoned"] == 0


# ---------------------------------------------------------------------
# organize_state (migration 0011 - see docs/LOW_CONFIDENCE_DESIGN.md)
# ---------------------------------------------------------------------

def test_get_organize_state_defaults_to_false_none_when_never_written(conn: sqlite3.Connection):
    assert db.get_organize_state(conn) == (False, None)


def test_set_organize_state_upserts(conn: sqlite3.Connection):
    db.set_organize_state(conn, True, "only 6 embedded chunk(s) - provisional", "2026-09-03T00:00:00+00:00")
    assert db.get_organize_state(conn) == (True, "only 6 embedded chunk(s) - provisional")

    # second write wins - single row, not an append-only log
    db.set_organize_state(conn, False, None, "2026-09-03T01:00:00+00:00")
    assert db.get_organize_state(conn) == (False, None)
    assert conn.execute("SELECT COUNT(*) FROM organize_state").fetchone()[0] == 1


# ---------------------------------------------------------------------
# sync_roots (migration 0013)
# ---------------------------------------------------------------------

def test_record_sync_root_sets_first_and_last_synced_at(conn: sqlite3.Connection):
    db.record_sync_root(conn, "filesystem", "/watched/notes", "2026-01-01T00:00:00")
    rows = db.get_sync_roots(conn)
    assert len(rows) == 1
    assert rows[0]["connector"] == "filesystem"
    assert rows[0]["root_id"] == "/watched/notes"
    assert rows[0]["first_synced_at"] == "2026-01-01T00:00:00"
    assert rows[0]["last_synced_at"] == "2026-01-01T00:00:00"


def test_record_sync_root_upsert_keeps_first_synced_at(conn: sqlite3.Connection):
    db.record_sync_root(conn, "filesystem", "/watched/notes", "2026-01-01T00:00:00")
    db.record_sync_root(conn, "filesystem", "/watched/notes", "2026-01-02T00:00:00")
    rows = db.get_sync_roots(conn)
    assert len(rows) == 1  # upsert, not a second row
    assert rows[0]["first_synced_at"] == "2026-01-01T00:00:00"  # unchanged
    assert rows[0]["last_synced_at"] == "2026-01-02T00:00:00"


def test_sync_roots_tracks_distinct_roots_per_connector(conn: sqlite3.Connection):
    db.record_sync_root(conn, "filesystem", "/watched/notes", "2026-01-01T00:00:00")
    db.record_sync_root(conn, "filesystem", "/watched/other", "2026-01-02T00:00:00")
    db.record_sync_root(conn, "drive", "folder-abc123", "2026-01-03T00:00:00")
    rows = db.get_sync_roots(conn)
    assert len(rows) == 3
    assert rows[0]["root_id"] == "folder-abc123"  # most recently synced first


def test_tombstone_sync_roots_hides_row_from_get_sync_roots(conn: sqlite3.Connection):
    db.record_sync_root(conn, "filesystem", "/watched/notes", "2026-01-01T00:00:00")
    db.record_sync_root(conn, "filesystem", "/watched", "2026-01-02T00:00:00")

    db.tombstone_sync_roots(conn, "filesystem", ["/watched/notes"])

    rows = db.get_sync_roots(conn)
    assert [row["root_id"] for row in rows] == ["/watched"]


def test_record_sync_root_un_tombstones_and_keeps_original_first_synced_at(conn: sqlite3.Connection):
    db.record_sync_root(conn, "filesystem", "/watched/notes", "2026-01-01T00:00:00")
    db.tombstone_sync_roots(conn, "filesystem", ["/watched/notes"])
    assert db.get_sync_roots(conn) == []

    db.record_sync_root(conn, "filesystem", "/watched/notes", "2026-02-01T00:00:00")

    rows = db.get_sync_roots(conn)
    assert len(rows) == 1
    assert rows[0]["first_synced_at"] == "2026-01-01T00:00:00"  # preserved
    assert rows[0]["last_synced_at"] == "2026-02-01T00:00:00"


# ---------------------------------------------------------------------
# ingest_runs + ingest_run_phases (migration 0016)
# ---------------------------------------------------------------------

def test_get_ingest_run_returns_none_when_never_written(conn: sqlite3.Connection):
    assert db.get_ingest_run(conn, "run-1") is None


def test_insert_and_finish_ingest_run(conn: sqlite3.Connection):
    db.insert_ingest_run(conn, "run-1", "filesystem", "/watched", "gui", "2026-09-21T00:00:00+00:00")
    row = db.get_ingest_run(conn, "run-1")
    assert row["status"] == db.RUN_STATUS_IN_PROGRESS
    assert row["connector"] == "filesystem"
    assert row["root_id"] == "/watched"
    assert row["trigger"] == "gui"
    assert row["ended_at"] is None

    db.finish_ingest_run(conn, "run-1", db.RUN_STATUS_DONE, "2026-09-21T00:01:00+00:00")
    row = db.get_ingest_run(conn, "run-1")
    assert row["status"] == db.RUN_STATUS_DONE
    assert row["ended_at"] == "2026-09-21T00:01:00+00:00"


def test_get_recent_ingest_runs_orders_most_recent_first(conn: sqlite3.Connection):
    db.insert_ingest_run(conn, "run-1", "filesystem", "/a", "gui", "2026-09-21T00:00:00+00:00")
    db.insert_ingest_run(conn, "run-2", "filesystem", "/a", "gui", "2026-09-21T00:01:00+00:00")
    rows = db.get_recent_ingest_runs(conn)
    assert [row["id"] for row in rows] == ["run-2", "run-1"]


def test_mark_orphaned_ingest_runs_interrupted_only_touches_in_progress_rows(conn: sqlite3.Connection):
    db.insert_ingest_run(conn, "run-1", "filesystem", "/a", "gui", "2026-09-21T00:00:00+00:00")
    db.insert_ingest_run(conn, "run-2", "filesystem", "/a", "gui", "2026-09-21T00:01:00+00:00")
    db.finish_ingest_run(conn, "run-2", db.RUN_STATUS_DONE, "2026-09-21T00:02:00+00:00")

    swept = db.mark_orphaned_ingest_runs_interrupted(conn, "2026-09-21T01:00:00+00:00")
    assert swept == 1
    assert db.get_ingest_run(conn, "run-1")["status"] == db.RUN_STATUS_INTERRUPTED
    assert db.get_ingest_run(conn, "run-2")["status"] == db.RUN_STATUS_DONE  # untouched


def test_ingest_run_phase_lifecycle(conn: sqlite3.Connection):
    db.insert_ingest_run(conn, "run-1", "filesystem", "/a", "gui", "2026-09-21T00:00:00+00:00")
    db.insert_ingest_run_phase(conn, "run-1", "embed", "2026-09-21T00:00:01+00:00")

    db.set_ingest_run_phase_progress(conn, "run-1", "embed", 500, 100, 5, 1, "2026-09-21T00:00:02+00:00")
    rows = db.get_ingest_run_phases(conn, "run-1")
    assert len(rows) == 1  # upserts in place, not an append-only log per batch
    assert rows[0]["status"] == db.PHASE_STATUS_IN_PROGRESS
    assert rows[0]["processed_items"] == 100

    db.finish_ingest_run_phase(conn, "run-1", "embed", db.PHASE_STATUS_DONE, "2026-09-21T00:00:03+00:00")
    row = db.get_ingest_run_phases(conn, "run-1")[0]
    assert row["status"] == db.PHASE_STATUS_DONE
    assert row["processed_items"] == 100  # left as-is - finish_ingest_run_phase passed no override
    assert row["ended_at"] == "2026-09-21T00:00:03+00:00"


def test_finish_ingest_run_phase_can_set_counts_for_an_unbatched_phase(conn: sqlite3.Connection):
    """chunk/sync/organize/reconcile never call set_ingest_run_phase_progress
    (only embed does) - they report a count, if any, directly on finish."""
    db.insert_ingest_run(conn, "run-1", "filesystem", "/a", "gui", "2026-09-21T00:00:00+00:00")
    db.insert_ingest_run_phase(conn, "run-1", "chunk", "2026-09-21T00:00:01+00:00")
    db.finish_ingest_run_phase(
        conn, "run-1", "chunk", db.PHASE_STATUS_DONE, "2026-09-21T00:00:02+00:00",
        total_items=12, processed_items=12,
    )
    row = db.get_ingest_run_phases(conn, "run-1")[0]
    assert row["total_items"] == 12
    assert row["processed_items"] == 12


def test_a_phase_never_started_has_no_row(conn: sqlite3.Connection):
    """Absence is the "skipped" signal (organize/reconcile on a no-op
    re-sync) - no separate status value for it."""
    db.insert_ingest_run(conn, "run-1", "filesystem", "/a", "gui", "2026-09-21T00:00:00+00:00")
    assert db.get_ingest_run_phases(conn, "run-1") == []


# ---------------------------------------------------------------------
# tool_calls (migration 0017)
# ---------------------------------------------------------------------

def test_insert_tool_call_prunes_to_max_rows(conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch):
    from jarvis_server.internal.db import tool_calls

    monkeypatch.setattr(tool_calls, "_MAX_ROWS", 5)
    for i in range(8):
        db.insert_tool_call(
            conn, "mcp_tool", "search", {"query": f"q{i}"}, {"results": []}, True,
            f"2026-09-21T00:00:{i:02d}+00:00",
        )
    rows = conn.execute("SELECT id FROM tool_calls ORDER BY id DESC").fetchall()
    assert len(rows) == 5
    assert [row["id"] for row in rows] == [8, 7, 6, 5, 4]  # 5 most recent by id


def test_insert_tool_call_truncates_oversized_payload(conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch):
    from jarvis_server.internal.db import tool_calls

    monkeypatch.setattr(tool_calls, "_MAX_FIELD_BYTES", 20)
    db.insert_tool_call(
        conn, "mcp_tool", "search", {"query": "x" * 100}, {"results": "y" * 100}, True,
        "2026-09-21T00:00:00+00:00",
    )
    row = conn.execute("SELECT request, response FROM tool_calls").fetchone()
    assert row["request"].endswith("...<truncated>")
    assert row["response"].endswith("...<truncated>")
    assert len(row["request"]) == 20 + len("...<truncated>")


def test_get_recent_tool_calls_pagination(conn: sqlite3.Connection):
    for i in range(5):
        db.insert_tool_call(
            conn, "mcp_tool", "search", {"query": f"q{i}"}, {"results": []}, True,
            f"2026-09-21T00:00:{i:02d}+00:00",
        )
    rows = db.get_recent_tool_calls(conn, limit=2)
    assert [row["id"] for row in rows] == [5, 4]  # most recent first

    rows = db.get_recent_tool_calls(conn, limit=2, before_id=4)
    assert [row["id"] for row in rows] == [3, 2]  # excludes rows at/after before_id


def test_insert_tool_call_stores_error_fields(conn: sqlite3.Connection):
    db.insert_tool_call(
        conn, "llm", "cluster_label", {"prompt": "p"}, {}, False,
        "2026-09-21T00:00:00+00:00", error_message="timeout", duration_ms=1500,
    )
    row = conn.execute("SELECT * FROM tool_calls").fetchone()
    assert row["success"] == 0
    assert row["error_message"] == "timeout"
    assert row["duration_ms"] == 1500


# ---------------------------------------------------------------------
# file_type_stats (migration 0018)
# ---------------------------------------------------------------------

def test_upsert_file_type_stats_accumulates_across_calls(conn: sqlite3.Connection):
    db.upsert_file_type_stats(
        conn,
        {".md": {"supported": True, "tried": 3, "skipped": 1, "failed": 0, "ignored": 0, "bytes": 100}},
        "2026-09-21T00:00:00+00:00",
    )
    db.upsert_file_type_stats(
        conn,
        {".md": {"supported": True, "tried": 2, "skipped": 0, "failed": 1, "ignored": 0, "bytes": 50}},
        "2026-09-21T01:00:00+00:00",
    )
    rows = db.get_file_type_stats(conn)
    assert len(rows) == 1  # upsert, not a second row
    row = rows[0]
    assert row["tried_count"] == 5  # sum of both calls, not the second call's value
    assert row["skipped_count"] == 1
    assert row["failed_count"] == 1
    assert row["bytes_processed"] == 150
    assert row["last_updated_at"] == "2026-09-21T01:00:00+00:00"


def test_get_processing_totals_sums_across_extensions(conn: sqlite3.Connection):
    db.upsert_file_type_stats(
        conn,
        {".md": {"supported": True, "tried": 3, "skipped": 0, "failed": 0, "ignored": 0, "bytes": 100}},
        "2026-09-21T00:00:00+00:00",
    )
    db.upsert_file_type_stats(
        conn,
        {".pdf": {"supported": True, "tried": 2, "skipped": 0, "failed": 0, "ignored": 0, "bytes": 400}},
        "2026-09-21T00:01:00+00:00",
    )
    totals = db.get_processing_totals(conn)
    assert totals["total_count"] == 5
    assert totals["total_bytes"] == 500


def test_get_processing_totals_defaults_to_zero_when_empty(conn: sqlite3.Connection):
    totals = db.get_processing_totals(conn)
    assert totals["total_count"] == 0
    assert totals["total_bytes"] == 0


def test_finish_ingest_run_records_the_error_code(conn: sqlite3.Connection):
    db.insert_ingest_run(conn, "run-1", "drive", "folder", "gui", "2026-10-07T00:00:00+00:00")
    db.finish_ingest_run(
        conn, "run-1", db.RUN_STATUS_FAILED, "2026-10-07T00:01:00+00:00", error_code="drive_auth_expired",
    )
    assert db.get_ingest_run(conn, "run-1")["error_code"] == "drive_auth_expired"
    assert db.get_recent_ingest_runs(conn)[0]["error_code"] == "drive_auth_expired"


def test_ingest_run_file_errors_round_trip_and_keep_the_first_reason(conn: sqlite3.Connection):
    db.insert_ingest_run(conn, "run-1", "filesystem", "/a", "gui", "2026-10-07T00:00:00+00:00")
    db.insert_ingest_run_file_errors(conn, "run-1", [
        {"source_id": "/a/b.pdf", "display_name": "b.pdf", "error_code": "file_read_failed"},
        {"source_id": "/a/a.docx", "display_name": "a.docx", "error_code": "network_unavailable"},
        {"source_id": "/a/b.pdf", "display_name": "b.pdf", "error_code": "unknown"},
    ])
    rows = [tuple(r) for r in db.get_ingest_run_file_errors(conn, "run-1")]
    assert rows == [
        ("/a/a.docx", "a.docx", "network_unavailable"),
        ("/a/b.pdf", "b.pdf", "file_read_failed"),
    ]
    assert db.get_ingest_run_file_errors(conn, "other-run") == []


def test_record_sync_root_keeps_a_display_name_until_a_new_one_arrives(conn: sqlite3.Connection):
    db.record_sync_root(conn, "drive", "folder-abc123", "2026-01-01T00:00:00", display_name="Team docs")
    db.record_sync_root(conn, "drive", "folder-abc123", "2026-01-02T00:00:00")
    assert db.get_sync_roots(conn)[0]["display_name"] == "Team docs"

    db.record_sync_root(conn, "drive", "folder-abc123", "2026-01-03T00:00:00", display_name="Renamed")
    assert db.get_sync_roots(conn)[0]["display_name"] == "Renamed"
