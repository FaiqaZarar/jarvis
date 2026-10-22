"""JARVIS_DB_PATH / JARVIS_GRAPH_PATH / JARVIS_DAEMON_HOME precedence.

jarvis/internal/db/connection.py's DEFAULT_DB_PATH and
jarvis/internal/graph_db/graph_db.py's DEFAULT_GRAPH_PATH are both
computed once at import time, so the derivation logic itself is
refactored into a plain function (`_default_db_path`/
`_default_graph_path`) specifically so it can be exercised directly
here, with monkeypatched env vars, without needing a fresh subprocess
per case.

Precedence, in order: the specific env var (JARVIS_DB_PATH /
JARVIS_GRAPH_PATH) wins outright when set, even alongside
JARVIS_DAEMON_HOME - existing tests (tests/test_daemon.py's
isolated_daemon_env) already set JARVIS_DB_PATH independently of
JARVIS_DAEMON_HOME and must keep working unchanged. JARVIS_DAEMON_HOME
is only a fallback for when the specific var isn't set.
"""

import pytest

from jarvis_server.internal.db import connection
from jarvis_server.internal.graph_db import graph_db


@pytest.fixture(autouse=True)
def _clear_relevant_env(monkeypatch):
    for var in ("JARVIS_DB_PATH", "JARVIS_GRAPH_PATH", "JARVIS_DAEMON_HOME"):
        monkeypatch.delenv(var, raising=False)


def test_db_path_uses_specific_env_var_when_set(monkeypatch, tmp_path):
    explicit = tmp_path / "explicit.db"
    monkeypatch.setenv("JARVIS_DB_PATH", str(explicit))

    assert connection._default_db_path() == explicit


def test_db_path_derives_from_daemon_home_when_specific_var_unset(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_DAEMON_HOME", str(tmp_path))

    assert connection._default_db_path() == tmp_path / "jarvis.db"


def test_db_path_prefers_specific_var_over_daemon_home(monkeypatch, tmp_path):
    """The one precedence case that must never regress: an existing test
    (isolated_daemon_env) sets JARVIS_DB_PATH to a name that does NOT
    match what JARVIS_DAEMON_HOME-derivation would produce
    (subprocess_test.db vs jarvis.db) - if derivation ever won here,
    that test's isolation would silently break."""
    explicit = tmp_path / "subprocess_test.db"
    monkeypatch.setenv("JARVIS_DB_PATH", str(explicit))
    monkeypatch.setenv("JARVIS_DAEMON_HOME", str(tmp_path))

    assert connection._default_db_path() == explicit
    assert connection._default_db_path() != tmp_path / "jarvis.db"


def test_db_path_falls_back_to_repo_relative_default_when_nothing_set():
    result = connection._default_db_path()

    assert result.name == "jarvis.db"


def test_graph_path_uses_specific_env_var_when_set(monkeypatch, tmp_path):
    explicit = tmp_path / "explicit_graph.lbdb"
    monkeypatch.setenv("JARVIS_GRAPH_PATH", str(explicit))

    assert graph_db._default_graph_path() == explicit


def test_graph_path_derives_from_daemon_home_when_specific_var_unset(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_DAEMON_HOME", str(tmp_path))

    assert graph_db._default_graph_path() == tmp_path / "jarvis_graph.lbdb"


def test_graph_path_prefers_specific_var_over_daemon_home(monkeypatch, tmp_path):
    explicit = tmp_path / "specific_graph.lbdb"
    monkeypatch.setenv("JARVIS_GRAPH_PATH", str(explicit))
    monkeypatch.setenv("JARVIS_DAEMON_HOME", str(tmp_path))

    assert graph_db._default_graph_path() == explicit
    assert graph_db._default_graph_path() != tmp_path / "jarvis_graph.lbdb"


def test_graph_path_falls_back_to_repo_relative_default_when_nothing_set():
    result = graph_db._default_graph_path()

    assert result.name == "jarvis_graph.lbdb"
