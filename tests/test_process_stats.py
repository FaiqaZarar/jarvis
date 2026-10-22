"""jarvis/modules/daemon/process_stats.py - local psutil/filesystem
introspection for the Settings page. Exercised against the test's own
process (no real daemon needed) and against nonexistent DB/graph paths
(the common case in a test environment, where neither file exists)."""

import os

from jarvis.modules.daemon import process_stats


def test_get_process_ram_returns_positive_int_for_own_process():
    ram = process_stats.get_process_ram(os.getpid())

    assert isinstance(ram, int)
    assert ram > 0


def test_get_daemon_uptime_seconds_returns_small_positive_float_for_own_process():
    uptime = process_stats.get_daemon_uptime_seconds(os.getpid())

    assert isinstance(uptime, float)
    assert uptime > 0


def test_get_process_ram_returns_none_for_dead_pid():
    # A pid no live process can plausibly hold - psutil.NoSuchProcess.
    assert process_stats.get_process_ram(2**30) is None


def test_get_storage_usage_does_not_raise_when_paths_are_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(process_stats, "DEFAULT_DB_PATH", tmp_path / "missing.db")
    monkeypatch.setattr(process_stats, "DEFAULT_GRAPH_PATH", tmp_path / "missing_graph.lbdb")

    usage = process_stats.get_storage_usage()

    assert usage == {"db_bytes": 0, "graph_bytes": 0, "total_bytes": 0}


def test_get_storage_usage_sums_existing_files(monkeypatch, tmp_path):
    db_path = tmp_path / "jarvis.db"
    graph_path = tmp_path / "jarvis_graph.lbdb"
    db_path.write_bytes(b"x" * 100)
    graph_path.write_bytes(b"y" * 250)
    monkeypatch.setattr(process_stats, "DEFAULT_DB_PATH", db_path)
    monkeypatch.setattr(process_stats, "DEFAULT_GRAPH_PATH", graph_path)

    usage = process_stats.get_storage_usage()

    assert usage == {"db_bytes": 100, "graph_bytes": 250, "total_bytes": 350}
