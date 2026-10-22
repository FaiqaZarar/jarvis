"""The real, end-to-end version of the orphaned-daemon fix (see
jarvis_server.daemon.server's module docstring for the full mechanism):
a real MainWindow, auto-starting a real daemon subprocess, registering
itself for real, then closing the window for real and confirming the
real daemon process actually exits - not a mocked stand-in for any of
those pieces.

test_daemon.py's own TestClient-based tests already cover the
register/shutdown route logic in isolation (fast, in-process), and its
own real-subprocess tests already cover "does the daemon actually die /
stay alive" without a GUI in the loop at all. This file is the one place
that proves the two halves are actually wired together correctly - the
thing neither of those, on its own, can prove.

Isolated the same way test_daemon.py's `isolated_daemon_env` fixture is
(JARVIS_DB_PATH/JARVIS_DAEMON_HOME pointed at tmp_path, and
jarvis.modules.daemon.client's own already-imported DISCOVERY_FILE/
LOG_FILE/_LOCK_FILE constants repointed to match, since a real daemon
subprocess reads the env vars fresh but this test process already
cached the old values at import time) - redefined here rather than
imported from test_daemon.py, matching this project's own convention of
per-file fixtures (route_db/client/conn are each redefined per file too,
not centralized)."""

import time

import pytest
from PySide6.QtCore import QThreadPool

import jarvis.modules.daemon.client as daemon_client
from jarvis_server.internal import env_config
from jarvis_server.internal.migrate import run_migrations
from jarvis.modules.cloud import session_store
from jarvis.modules.desktop_app import main_window as main_window_mod
from jarvis.modules.desktop_app.main_window import MainWindow


@pytest.fixture(autouse=True)
def _skip_first_run_sign_in(monkeypatch):
    # Not about the cloud sign-in dialog - same hang risk, same fix
    # shape as the has_embedding_api_key mock each test here already does.
    monkeypatch.setattr(session_store, "load_session", lambda: ("user@example.com", "tok"))


@pytest.fixture
def isolated_daemon_env(tmp_path, monkeypatch):
    db_path = tmp_path / "subprocess_test.db"
    run_migrations(db_path)

    monkeypatch.setenv("JARVIS_DB_PATH", str(db_path))
    monkeypatch.setenv("JARVIS_DAEMON_HOME", str(tmp_path))
    monkeypatch.setattr(daemon_client, "DISCOVERY_FILE", tmp_path / ".jarvis_daemon.json")
    monkeypatch.setattr(daemon_client, "LOG_FILE", tmp_path / ".jarvis_daemon.log")
    monkeypatch.setattr(daemon_client, "_LOCK_FILE", tmp_path / ".jarvis_daemon.lock")
    return tmp_path


def test_gui_shuts_down_its_own_real_daemon_on_clean_close(qtbot, isolated_daemon_env, monkeypatch):
    monkeypatch.setattr(env_config, "has_embedding_api_key", lambda: True)  # skip the first-run dialog

    window = MainWindow(QThreadPool())
    qtbot.addWidget(window)

    qtbot.waitUntil(lambda: window.search_view.isEnabled(), timeout=20000)  # real daemon auto-spawn
    qtbot.waitUntil(lambda: window._daemon_client_id is not None, timeout=10000)  # real registration round-trip

    info = daemon_client._read_discovery()
    assert info is not None and daemon_client._health_check(info["port"]), "no real daemon reached before close"

    window.close()

    # closeEvent's request_shutdown() call is synchronous, so by the
    # time close() returns the daemon has already been told to retire -
    # give it a short, generous window to actually exit its event loop.
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and daemon_client._health_check(info["port"]):
        time.sleep(0.2)

    assert not daemon_client._health_check(info["port"]), "the real daemon is still alive after a clean GUI close"


def test_gui_does_not_kill_a_real_daemon_still_serving_another_client(qtbot, isolated_daemon_env, monkeypatch):
    """The other half of the same real test: a daemon this window shares
    with another real, still-registered client (standing in for the CLI
    or a --mcp session, per the task's own "simulate via ... a second
    reference") must survive this window closing."""
    monkeypatch.setattr(env_config, "has_embedding_api_key", lambda: True)

    window = MainWindow(QThreadPool())
    qtbot.addWidget(window)

    qtbot.waitUntil(lambda: window.search_view.isEnabled(), timeout=20000)
    qtbot.waitUntil(lambda: window._daemon_client_id is not None, timeout=10000)

    info = daemon_client._read_discovery()
    assert info is not None and daemon_client._health_check(info["port"])

    other_client_id = daemon_client.register_client()  # a second, independent real client

    window.close()

    # Give the (correctly refused) shutdown request a moment to have not
    # happened, then confirm the daemon is still really there.
    time.sleep(1.0)
    assert daemon_client._health_check(info["port"]), "the real daemon died even though another client was still registered"

    # Cleanup: this test's daemon would otherwise be the one real orphan
    # left behind by the whole suite - retire the other client too.
    daemon_client.request_shutdown(other_client_id)
