"""First-run settings screen (docs/desktop-app-DESIGN.md §8.8): MainWindow's
branch on `env_config.has_embedding_api_key()`, and SettingsDialog's own
accept/validation logic.

`SettingsDialog.exec()` is a real, blocking modal event loop - every test
that constructs a `MainWindow` without a key configured mocks `.exec()`
so it never actually blocks, the same class of hang already found and
fixed once for a blocking failure dialog (see test_desktop_app_views.py).
"""

import pytest
from PySide6.QtCore import QThreadPool

from jarvis_server.internal import env_config
from jarvis.modules.cloud import session_store
from jarvis.modules.desktop_app import main_window as main_window_mod
from jarvis.modules.desktop_app.main_window import MainWindow
from jarvis.modules.desktop_app.settings_dialog import SettingsDialog


@pytest.fixture(autouse=True)
def _skip_first_run_sign_in_by_default(monkeypatch):
    """None of the tests below are about the cloud sign-in dialog (that's
    test_desktop_app_sign_in.py) - default every `MainWindow(...)`
    construction in this file to "already signed in" so the real
    `SignInDialog.exec()` never pops up and blocks, the same class of
    hang this file's own SettingsDialog mocking already guards against."""
    monkeypatch.setattr(session_store, "load_session", lambda: ("user@example.com", "tok"))


# ---------------------------------------------------------------------
# MainWindow's first-run branch
# ---------------------------------------------------------------------

def test_main_window_shows_settings_dialog_first_run_when_no_key_configured(qtbot, monkeypatch):
    monkeypatch.setattr(env_config, "has_embedding_api_key", lambda: False)
    monkeypatch.setattr(main_window_mod, "run_daemon_call", lambda *a, **k: None)

    shown = {"first_run": None, "exec_called": False}

    class _FakeDialog:
        def __init__(self, parent, *, first_run=False):
            shown["first_run"] = first_run

        def exec(self):
            shown["exec_called"] = True
            return 0  # Rejected - equivalent to Cancel

    monkeypatch.setattr(main_window_mod, "SettingsDialog", _FakeDialog)

    window = MainWindow(QThreadPool())
    qtbot.addWidget(window)

    assert shown["exec_called"] is True
    assert shown["first_run"] is True


def test_settings_account_label_refreshes_after_first_run_sign_in(qtbot, monkeypatch):
    """Real bug, found during manual testing: settings_view's Account
    section is built (and its label computed) *before* the first-run
    sign-in dialog ever runs - a successful sign-in there was leaving
    the page permanently stuck on "Not signed in" until the user
    happened to click the Account button itself. MainWindow must
    refresh that label once the first-run dialog closes."""
    monkeypatch.setattr(env_config, "has_embedding_api_key", lambda: True)
    monkeypatch.setattr(main_window_mod, "run_daemon_call", lambda *a, **k: None)

    signed_in = {"value": False}
    monkeypatch.setattr(
        session_store, "load_session",
        lambda: ("user@example.com", "tok") if signed_in["value"] else None,
    )

    class _FakeSignInDialog:
        def __init__(self, parent, pool, *, first_run=False):
            self.quit_requested = False

        def exec(self):
            signed_in["value"] = True  # simulate a completed sign-in
            return 1

    monkeypatch.setattr(main_window_mod, "SignInDialog", _FakeSignInDialog)

    window = MainWindow(QThreadPool())
    qtbot.addWidget(window)

    assert window.settings_view._account_label.text() == "Signed in as user@example.com"


def test_quitting_first_run_sign_in_skips_the_settings_dialog_too(qtbot, monkeypatch):
    """Regression test: clicking "Quit Jarvis" on the sign-in gate must
    stop MainWindow.__init__ right there - without this, it went on to
    show the embedding-API-key settings dialog anyway (a second first-run
    modal popping up right after Quit), which made Quit look like it did
    nothing at all."""
    monkeypatch.setattr(session_store, "load_session", lambda: None)
    monkeypatch.setattr(env_config, "has_embedding_api_key", lambda: False)

    class _FakeSignInDialog:
        def __init__(self, parent, pool, *, first_run=False):
            self.quit_requested = True

        def exec(self):
            return 0  # Rejected - the user quit

    def _boom(*a, **k):  # pragma: no cover - must never be called
        raise AssertionError("SettingsDialog must not be shown after the user quit")

    monkeypatch.setattr(main_window_mod, "SignInDialog", _FakeSignInDialog)
    monkeypatch.setattr(main_window_mod, "SettingsDialog", _boom)

    window = MainWindow(QThreadPool())
    qtbot.addWidget(window)
    # No assertion needed beyond "construction didn't raise" - _boom
    # would have raised AssertionError if SettingsDialog were touched.


def test_main_window_skips_settings_dialog_when_key_already_configured(qtbot, monkeypatch):
    monkeypatch.setattr(env_config, "has_embedding_api_key", lambda: True)
    monkeypatch.setattr(main_window_mod, "run_daemon_call", lambda *a, **k: None)

    def _boom(*a, **k):  # pragma: no cover - must never be called
        raise AssertionError("SettingsDialog must not be shown when a key is already configured")

    monkeypatch.setattr(main_window_mod, "SettingsDialog", _boom)

    window = MainWindow(QThreadPool())
    qtbot.addWidget(window)
    # No assertion needed beyond "construction didn't raise" - _boom
    # would have raised AssertionError if SettingsDialog were touched.


def test_main_window_proceeds_to_daemon_check_after_first_run_dialog_closes(qtbot, monkeypatch):
    """Whether the user saves a key or cancels, the existing daemon-check
    flow must still run afterward - the daemon doesn't need a key to
    start, only real embed/search calls do."""
    monkeypatch.setattr(env_config, "has_embedding_api_key", lambda: False)

    calls = []
    monkeypatch.setattr(main_window_mod, "run_daemon_call", lambda *a, **k: calls.append(1))
    monkeypatch.setattr(main_window_mod, "SettingsDialog", lambda parent, **k: type(
        "_D", (), {"exec": lambda self: 0}
    )())

    window = MainWindow(QThreadPool())
    qtbot.addWidget(window)

    assert len(calls) == 1  # _check_daemon() -> run_daemon_call ran exactly once


# ---------------------------------------------------------------------
# SettingsDialog's own logic - constructed directly, .exec() never
# called (accept/reject driven manually instead of via the modal loop).
# ---------------------------------------------------------------------

def test_settings_dialog_starts_empty_with_save_disabled_when_nothing_saved_yet(qtbot, tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_DAEMON_HOME", str(tmp_path))

    dialog = SettingsDialog(None, first_run=True)
    qtbot.addWidget(dialog)

    assert dialog._api_key.text() == ""
    assert dialog._save_button.isEnabled() is False


def test_settings_dialog_prefills_the_saved_key(qtbot, tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_DAEMON_HOME", str(tmp_path))
    env_config.save_settings("sk-existing", "a-model", "https://example.test", daemon_home=tmp_path)

    dialog = SettingsDialog(None)
    qtbot.addWidget(dialog)

    assert dialog._api_key.text() == "sk-existing"
    assert dialog._save_button.isEnabled() is True


def test_settings_dialog_accept_saves_and_closes(qtbot, tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_DAEMON_HOME", str(tmp_path))

    dialog = SettingsDialog(None, first_run=True)
    qtbot.addWidget(dialog)
    dialog._api_key.setText("sk-typed-in")

    dialog._save_button.click()

    assert dialog.result() == 1  # Accepted
    saved = env_config.read_saved_settings(tmp_path)
    assert saved["EMBEDDING_API_KEY"] == "sk-typed-in"
    assert saved["EMBEDDING_MODEL"] == env_config.DEFAULT_MODEL


def test_settings_dialog_keeps_the_saved_model_and_api_base(qtbot, tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_DAEMON_HOME", str(tmp_path))
    env_config.save_settings("sk-old", "a-model", "https://example.test", daemon_home=tmp_path)

    dialog = SettingsDialog(None)
    qtbot.addWidget(dialog)
    dialog._api_key.setText("sk-new")
    dialog._save_button.click()

    saved = env_config.read_saved_settings(tmp_path)
    assert saved["EMBEDDING_API_KEY"] == "sk-new"
    assert saved["EMBEDDING_MODEL"] == "a-model"
    assert saved["EMBEDDING_API_BASE"] == "https://example.test"


def test_settings_dialog_does_not_save_an_empty_key(qtbot, tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_DAEMON_HOME", str(tmp_path))

    dialog = SettingsDialog(None, first_run=True)
    qtbot.addWidget(dialog)
    dialog._api_key.setText("   ")

    dialog._on_accept()

    assert dialog._save_button.isEnabled() is False
    assert dialog.result() != 1
    assert not (tmp_path / ".env").exists()


def test_settings_dialog_uses_plain_words_and_save_and_cancel(qtbot, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QLabel, QLineEdit, QPushButton

    monkeypatch.setenv("JARVIS_DAEMON_HOME", str(tmp_path))
    dialog = SettingsDialog(None, first_run=True)
    qtbot.addWidget(dialog)

    assert dialog.windowTitle() == "Add an API key"
    assert [b.text() for b in dialog.findChildren(QPushButton)] == ["Cancel", "Save"]
    assert dialog.findChildren(QLineEdit) == [dialog._api_key]
    text = " ".join(label.text() for label in dialog.findChildren(QLabel))
    for jargon in ("OpenRouter", "embed", "Embedding", "never sent", "jarvis_daemon.log"):
        assert jargon not in text
    assert "Jarvis needs an API key to read your files." in text
