"""ConnectClaudeDesktopDialog - the copy-to-clipboard and open-folder
buttons, and the fragment-vs-whole-file safety distinction in the copy
button's own label and warning text (see claude_desktop_dialog.py's
module docstring for why that distinction matters).

Every test controls `claude_desktop_setup.build_plan()`'s output by
monkeypatching the two functions it calls internally
(`default_config_path`, `frozen_mcp_entry`) - the same style
`test_desktop_app_settings.py` uses for `env_config`, rather than
reaching into the dialog's own internals."""

import json
import os
import sys

from PySide6.QtWidgets import QApplication, QMessageBox

import jarvis.modules.desktop_app.claude_desktop_setup as cds
from jarvis.modules.desktop_app import claude_desktop_dialog as dialog_mod
from jarvis.modules.desktop_app.claude_desktop_dialog import ConnectClaudeDesktopDialog

_ENTRY = {"command": r"C:\Program Files\Jarvis\jarvis_desktop.exe", "args": ["--mcp"]}


# ---------------------------------------------------------------------
# "Add automatically" - the primary, confirmed, backed-up write path.
# QMessageBox.question/information/critical are monkeypatched to
# avoid a real blocking modal, matching DriveFolderDialog's own tests
# for the same reason.
# ---------------------------------------------------------------------

def test_apply_button_present_when_full_config_is_available(qtbot, monkeypatch, tmp_path):
    path = tmp_path / "claude_desktop_config.json"
    monkeypatch.setattr(cds, "default_config_path", lambda: path)
    monkeypatch.setattr(cds, "frozen_mcp_entry", lambda: _ENTRY)

    dialog = ConnectClaudeDesktopDialog()
    qtbot.addWidget(dialog)

    assert dialog._apply_button is not None
    assert "automatically" in dialog._apply_button.text().lower()


def test_apply_button_absent_when_full_config_is_none(qtbot, monkeypatch, tmp_path):
    path = tmp_path / "claude_desktop_config.json"
    path.write_text("{ not valid json ][")  # CASE_UNREADABLE - full_config is None
    monkeypatch.setattr(cds, "default_config_path", lambda: path)
    monkeypatch.setattr(cds, "frozen_mcp_entry", lambda: _ENTRY)

    dialog = ConnectClaudeDesktopDialog()
    qtbot.addWidget(dialog)

    assert dialog._apply_button is None


def test_apply_button_absent_in_dev_mode_and_unsupported_platform(qtbot, monkeypatch, tmp_path):
    monkeypatch.setattr(cds, "default_config_path", lambda: tmp_path / "claude_desktop_config.json")
    monkeypatch.setattr(cds, "frozen_mcp_entry", lambda: None)
    dev_dialog = ConnectClaudeDesktopDialog()
    qtbot.addWidget(dev_dialog)
    assert dev_dialog._apply_button is None

    monkeypatch.setattr(cds, "default_config_path", lambda: None)
    unsupported_dialog = ConnectClaudeDesktopDialog()
    qtbot.addWidget(unsupported_dialog)
    assert unsupported_dialog._apply_button is None


def test_confirming_apply_writes_the_file_and_closes_the_dialog(qtbot, monkeypatch, tmp_path):
    path = tmp_path / "claude_desktop_config.json"
    path.write_text(json.dumps({"coworkUserFilesPath": "x"}))
    monkeypatch.setattr(cds, "default_config_path", lambda: path)
    monkeypatch.setattr(cds, "frozen_mcp_entry", lambda: _ENTRY)
    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes))
    monkeypatch.setattr(QMessageBox, "information", staticmethod(lambda *a, **k: None))

    dialog = ConnectClaudeDesktopDialog()
    qtbot.addWidget(dialog)

    dialog._apply_button.click()

    written = json.loads(path.read_text())
    assert written["coworkUserFilesPath"] == "x"
    assert written["mcpServers"]["jarvis"] == _ENTRY
    assert path.with_name(path.name + ".bak").exists()
    assert dialog.result() == dialog_mod.QDialog.DialogCode.Accepted


def test_declining_the_apply_confirmation_does_not_touch_the_file(qtbot, monkeypatch, tmp_path):
    path = tmp_path / "claude_desktop_config.json"
    original = json.dumps({"coworkUserFilesPath": "x"})
    path.write_text(original)
    monkeypatch.setattr(cds, "default_config_path", lambda: path)
    monkeypatch.setattr(cds, "frozen_mcp_entry", lambda: _ENTRY)
    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.StandardButton.No))

    dialog = ConnectClaudeDesktopDialog()
    qtbot.addWidget(dialog)

    dialog._apply_button.click()

    assert path.read_text() == original
    assert dialog.result() != dialog_mod.QDialog.DialogCode.Accepted


def test_apply_confirmation_mentions_the_path_and_backup_when_file_exists(qtbot, monkeypatch, tmp_path):
    path = tmp_path / "claude_desktop_config.json"
    path.write_text(json.dumps({"mcpServers": {}}))
    monkeypatch.setattr(cds, "default_config_path", lambda: path)
    monkeypatch.setattr(cds, "frozen_mcp_entry", lambda: _ENTRY)

    captured = {}
    def _fake_question(self, title, text, *a, **k):
        captured["text"] = text
        return QMessageBox.StandardButton.No
    monkeypatch.setattr(QMessageBox, "question", _fake_question)

    dialog = ConnectClaudeDesktopDialog()
    qtbot.addWidget(dialog)
    dialog._apply_button.click()

    assert str(path) in captured["text"]
    assert ".bak" in captured["text"]
    assert "restart" in captured["text"].lower()


def test_apply_confirmation_does_not_mention_a_backup_when_file_is_missing(qtbot, monkeypatch, tmp_path):
    path = tmp_path / "claude_desktop_config.json"
    monkeypatch.setattr(cds, "default_config_path", lambda: path)
    monkeypatch.setattr(cds, "frozen_mcp_entry", lambda: _ENTRY)

    captured = {}
    def _fake_question(self, title, text, *a, **k):
        captured["text"] = text
        return QMessageBox.StandardButton.No
    monkeypatch.setattr(QMessageBox, "question", _fake_question)

    dialog = ConnectClaudeDesktopDialog()
    qtbot.addWidget(dialog)
    dialog._apply_button.click()

    assert ".bak" not in captured["text"]


def test_apply_failure_shows_an_error_and_does_not_close_the_dialog(qtbot, monkeypatch, tmp_path):
    path = tmp_path / "claude_desktop_config.json"
    monkeypatch.setattr(cds, "default_config_path", lambda: path)
    monkeypatch.setattr(cds, "frozen_mcp_entry", lambda: _ENTRY)
    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes))

    def _boom(plan):
        raise OSError("permission denied")
    monkeypatch.setattr(cds, "apply_plan", _boom)

    critical_calls = []
    monkeypatch.setattr(QMessageBox, "critical", staticmethod(lambda *a, **k: critical_calls.append(a)))

    dialog = ConnectClaudeDesktopDialog()
    qtbot.addWidget(dialog)

    dialog._apply_button.click()

    assert len(critical_calls) == 1
    assert dialog.result() != dialog_mod.QDialog.DialogCode.Accepted
    assert not path.exists()  # apply_plan was mocked to fail before writing anything


# ---------------------------------------------------------------------
# Copy button - real clipboard content, exact match
# ---------------------------------------------------------------------

def test_copy_button_copies_the_exact_generated_snippet_missing_file_case(qtbot, monkeypatch, tmp_path):
    path = tmp_path / "claude_desktop_config.json"
    monkeypatch.setattr(cds, "default_config_path", lambda: path)
    monkeypatch.setattr(cds, "frozen_mcp_entry", lambda: _ENTRY)

    dialog = ConnectClaudeDesktopDialog()
    qtbot.addWidget(dialog)

    dialog._copy_button.click()

    assert QApplication.clipboard().text() == dialog._plan.snippet
    assert json.loads(QApplication.clipboard().text()) == {"mcpServers": {"jarvis": _ENTRY}}


def test_copy_button_copies_the_exact_generated_snippet_fragment_case(qtbot, monkeypatch, tmp_path):
    path = tmp_path / "claude_desktop_config.json"
    path.write_text(json.dumps({"mcpServers": {"other": {"command": "x", "args": []}}}))
    monkeypatch.setattr(cds, "default_config_path", lambda: path)
    monkeypatch.setattr(cds, "frozen_mcp_entry", lambda: _ENTRY)

    dialog = ConnectClaudeDesktopDialog()
    qtbot.addWidget(dialog)

    dialog._copy_button.click()

    assert QApplication.clipboard().text() == dialog._plan.snippet
    # A real fragment, not a full file - confirm it doesn't stand alone.
    assert json.loads("{\"mcpServers\": {" + QApplication.clipboard().text() + "}}") == {
        "mcpServers": {"jarvis": _ENTRY}
    }


# ---------------------------------------------------------------------
# Open-folder button - only os.startfile is mocked; the real mkdir runs.
# ---------------------------------------------------------------------

def test_open_folder_button_creates_the_folder_and_calls_os_startfile(qtbot, monkeypatch, tmp_path):
    target_dir = tmp_path / "Claude"
    path = target_dir / "claude_desktop_config.json"
    monkeypatch.setattr(cds, "default_config_path", lambda: path)
    monkeypatch.setattr(cds, "frozen_mcp_entry", lambda: _ENTRY)
    assert not target_dir.exists()

    captured = {}
    monkeypatch.setattr(dialog_mod.os, "startfile", lambda p: captured.setdefault("path", p))

    dialog = ConnectClaudeDesktopDialog()
    qtbot.addWidget(dialog)

    dialog._open_folder_button.click()

    assert target_dir.exists()  # the real mkdir ran, not mocked
    assert captured["path"] == str(target_dir)


def test_open_folder_button_uses_the_open_command_on_macos(qtbot, monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "darwin")
    target_dir = tmp_path / "Claude"
    path = target_dir / "claude_desktop_config.json"
    monkeypatch.setattr(cds, "default_config_path", lambda: path)
    monkeypatch.setattr(cds, "frozen_mcp_entry", lambda: _ENTRY)
    assert not target_dir.exists()

    captured = {}
    monkeypatch.setattr(dialog_mod.subprocess, "run", lambda args: captured.setdefault("args", args))

    dialog = ConnectClaudeDesktopDialog()
    qtbot.addWidget(dialog)

    dialog._open_folder_button.click()

    assert target_dir.exists()  # the real mkdir ran, not mocked
    assert captured["args"] == ["open", str(target_dir)]


def test_open_folder_button_never_creates_the_config_file_itself(qtbot, monkeypatch, tmp_path):
    target_dir = tmp_path / "Claude"
    path = target_dir / "claude_desktop_config.json"
    monkeypatch.setattr(cds, "default_config_path", lambda: path)
    monkeypatch.setattr(cds, "frozen_mcp_entry", lambda: _ENTRY)
    monkeypatch.setattr(dialog_mod.os, "startfile", lambda p: None)

    dialog = ConnectClaudeDesktopDialog()
    qtbot.addWidget(dialog)
    dialog._open_folder_button.click()

    assert not path.exists()


def test_open_folder_button_disabled_on_unsupported_platform(qtbot, monkeypatch):
    monkeypatch.setattr(cds, "default_config_path", lambda: None)
    monkeypatch.setattr(cds, "frozen_mcp_entry", lambda: _ENTRY)

    dialog = ConnectClaudeDesktopDialog()
    qtbot.addWidget(dialog)

    assert dialog._open_folder_button.isEnabled() is False


def test_open_folder_button_still_enabled_in_dev_mode(qtbot, monkeypatch, tmp_path):
    """Dev mode has no snippet to copy, but the config path is still
    real and known - opening the folder is still useful."""
    path = tmp_path / "claude_desktop_config.json"
    monkeypatch.setattr(cds, "default_config_path", lambda: path)
    monkeypatch.setattr(cds, "frozen_mcp_entry", lambda: None)

    dialog = ConnectClaudeDesktopDialog()
    qtbot.addWidget(dialog)

    assert dialog._copy_button is None
    assert dialog._open_folder_button.isEnabled() is True


# ---------------------------------------------------------------------
# The safety-critical fragment vs. whole-file distinction
# ---------------------------------------------------------------------

def test_missing_file_case_button_says_file_contents_not_fragment(qtbot, monkeypatch, tmp_path):
    path = tmp_path / "claude_desktop_config.json"
    monkeypatch.setattr(cds, "default_config_path", lambda: path)
    monkeypatch.setattr(cds, "frozen_mcp_entry", lambda: _ENTRY)

    dialog = ConnectClaudeDesktopDialog()
    qtbot.addWidget(dialog)

    assert dialog._copy_button.text() == "Copy file contents to clipboard"
    assert "fragment" not in dialog._copy_button.text().lower()


def test_no_mcp_servers_case_button_and_warning_say_fragment(qtbot, monkeypatch, tmp_path):
    from PySide6.QtWidgets import QLabel

    path = tmp_path / "claude_desktop_config.json"
    path.write_text(json.dumps({"coworkUserFilesPath": "x"}))
    monkeypatch.setattr(cds, "default_config_path", lambda: path)
    monkeypatch.setattr(cds, "frozen_mcp_entry", lambda: _ENTRY)

    dialog = ConnectClaudeDesktopDialog()
    qtbot.addWidget(dialog)

    assert dialog._copy_button.text() == "Copy fragment to clipboard"
    labels = [w.text() for w in dialog.findChildren(QLabel)]
    assert any("do not replace the whole file" in t.lower() for t in labels)


def test_has_mcp_servers_case_button_and_warning_say_fragment(qtbot, monkeypatch, tmp_path):
    from PySide6.QtWidgets import QLabel

    path = tmp_path / "claude_desktop_config.json"
    path.write_text(json.dumps({"mcpServers": {"other": {"command": "x", "args": []}}}))
    monkeypatch.setattr(cds, "default_config_path", lambda: path)
    monkeypatch.setattr(cds, "frozen_mcp_entry", lambda: _ENTRY)

    dialog = ConnectClaudeDesktopDialog()
    qtbot.addWidget(dialog)

    assert dialog._copy_button.text() == "Copy fragment to clipboard"
    labels = [w.text() for w in dialog.findChildren(QLabel)]
    assert any("do not replace the whole file" in t.lower() for t in labels)


def test_dev_mode_and_unsupported_platform_have_no_copy_button(qtbot, monkeypatch, tmp_path):
    monkeypatch.setattr(cds, "default_config_path", lambda: tmp_path / "claude_desktop_config.json")
    monkeypatch.setattr(cds, "frozen_mcp_entry", lambda: None)
    dev_dialog = ConnectClaudeDesktopDialog()
    qtbot.addWidget(dev_dialog)
    assert dev_dialog._copy_button is None

    monkeypatch.setattr(cds, "default_config_path", lambda: None)
    unsupported_dialog = ConnectClaudeDesktopDialog()
    qtbot.addWidget(unsupported_dialog)
    assert unsupported_dialog._copy_button is None


def test_the_by_hand_steps_start_folded_away_when_jarvis_can_connect_itself(qtbot, monkeypatch, tmp_path):
    path = tmp_path / "claude_desktop_config.json"
    monkeypatch.setattr(cds, "default_config_path", lambda: path)
    monkeypatch.setattr(cds, "frozen_mcp_entry", lambda: _ENTRY)

    dialog = ConnectClaudeDesktopDialog()
    qtbot.addWidget(dialog)

    assert dialog._apply_button.property("buttonKind") == "primary"
    assert dialog._manual_section.isHidden()
    dialog._manual_toggle.click()
    assert not dialog._manual_section.isHidden()


def test_the_snippet_keeps_its_line_breaks(qtbot, monkeypatch, tmp_path):
    from PySide6.QtWidgets import QPlainTextEdit

    path = tmp_path / "claude_desktop_config.json"
    monkeypatch.setattr(cds, "default_config_path", lambda: path)
    monkeypatch.setattr(cds, "frozen_mcp_entry", lambda: _ENTRY)

    dialog = ConnectClaudeDesktopDialog()
    qtbot.addWidget(dialog)

    (snippet_field,) = dialog.findChildren(QPlainTextEdit)
    assert snippet_field.toPlainText() == dialog._plan.snippet
    assert "\n" in snippet_field.toPlainText()
