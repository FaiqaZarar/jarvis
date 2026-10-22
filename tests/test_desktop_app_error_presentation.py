"""Tests for error_presentation's ConfirmDialog/confirm_action (DESIGN.md
§4.10) - `exec` is patched to inspect the real dialog before any blocking
event loop would run - and for DaemonStatusBanner (§4.2)."""

from PySide6.QtWidgets import QDialog, QHBoxLayout, QLabel

from jarvis.modules.desktop_app.error_presentation import (
    ConfirmDialog,
    DaemonStatusBanner,
    banner_markup,
    confirm_action,
)


def _capture_dialog(monkeypatch, accept: bool = True):
    captured = {}

    def fake_exec(self):
        captured["dialog"] = self
        return QDialog.DialogCode.Accepted if accept else QDialog.DialogCode.Rejected

    monkeypatch.setattr(ConfirmDialog, "exec", fake_exec)
    return captured


def test_title_is_a_bold_heading_and_the_message_its_consequence(qtbot):
    dialog = ConfirmDialog(None, "Remove “Notes”?", "Its files will no longer appear in search.", "Remove")
    qtbot.addWidget(dialog)

    assert dialog.findChild(QLabel, "dialogTitle").text() == "Remove “Notes”?"
    assert dialog.findChild(QLabel, "confirmDialogMessage").text() == "Its files will no longer appear in search."


def test_the_action_sits_right_of_cancel(qtbot):
    dialog = ConfirmDialog(None, "Restart?", "...", "Restart")
    qtbot.addWidget(dialog)

    buttons = next(item.layout() for item in (
        dialog.layout().itemAt(i) for i in range(dialog.layout().count())
    ) if isinstance(item.layout(), QHBoxLayout))
    order = [buttons.itemAt(i).widget() for i in range(buttons.count()) if buttons.itemAt(i).widget()]
    assert order == [dialog.cancel_button, dialog.action_button]


def test_destructive_tags_the_action_button_as_destructive(qtbot):
    dialog = ConfirmDialog(None, "Force kill?", "...", "Force kill", destructive=True)
    qtbot.addWidget(dialog)

    assert dialog.action_button.property("buttonKind") == "destructive"
    assert dialog.cancel_button.property("buttonKind") in (None, "")


def test_non_destructive_action_is_the_primary_button(qtbot):
    dialog = ConfirmDialog(None, "Restart?", "...", "Restart")
    qtbot.addWidget(dialog)

    assert dialog.action_button.property("buttonKind") == "primary"


def test_action_button_is_labelled_with_the_action(qtbot):
    dialog = ConfirmDialog(None, "Sync folder?", "This downloads its files.", "Sync")
    qtbot.addWidget(dialog)

    assert dialog.action_button.text() == "Sync"
    assert dialog.cancel_button.text() == "Cancel"


def test_cancel_is_the_default_button(qtbot):
    # DESIGN.md §4.10: keyboard focus starts on the safe action.
    dialog = ConfirmDialog(None, "Force kill?", "...", "Force kill", destructive=True)
    qtbot.addWidget(dialog)

    assert dialog.cancel_button.isDefault()
    assert not dialog.action_button.isDefault()


def test_returns_true_only_when_the_action_is_chosen(qtbot, monkeypatch):
    _capture_dialog(monkeypatch, accept=True)
    assert confirm_action(None, "Restart?", "...", "Restart") is True

    _capture_dialog(monkeypatch, accept=False)
    assert confirm_action(None, "Restart?", "...", "Restart") is False


def test_cancel_and_action_buttons_close_the_dialog_with_the_matching_result(qtbot):
    dialog = ConfirmDialog(None, "Restart?", "...", "Restart")
    qtbot.addWidget(dialog)
    dialog.action_button.click()
    assert dialog.result() == QDialog.DialogCode.Accepted

    dialog = ConfirmDialog(None, "Restart?", "...", "Restart")
    qtbot.addWidget(dialog)
    dialog.cancel_button.click()
    assert dialog.result() == QDialog.DialogCode.Rejected


def test_banner_markup_bolds_only_the_first_sentence():
    markup = banner_markup("All caught up. 3 files saved.")

    assert markup == "<span style='font-weight:600'>All caught up.</span> 3 files saved."


def test_banner_markup_bolds_a_one_sentence_message_whole_and_escapes_it():
    assert banner_markup("Removing <a>…") == "<span style='font-weight:600'>Removing &lt;a&gt;…</span>"


def test_banner_markup_ignores_a_full_stop_inside_a_quoted_name():
    markup = banner_markup("Syncing “Q3. Final”. You can keep using Jarvis.")

    assert markup == (
        "<span style='font-weight:600'>Syncing “Q3. Final”.</span> You can keep using Jarvis."
    )


def test_banner_keeps_the_plain_message(qtbot):
    banner = DaemonStatusBanner()
    qtbot.addWidget(banner)

    banner.show_info("Syncing “Notes”. You can keep using Jarvis.")

    assert banner.message() == "Syncing “Notes”. You can keep using Jarvis."
    assert "font-weight:600" in banner._label.text()


def test_errors_stay_but_info_and_success_can_be_dismissed_when_errors_are_not_dismissible(qtbot):
    banner = DaemonStatusBanner(errors_dismissible=False)
    qtbot.addWidget(banner)

    banner.show_error("Mostly done. 1 file couldn't be read.")
    assert banner._dismiss_button.isHidden()

    banner.show_info("Syncing “Notes”. You can keep using Jarvis.")
    assert not banner._dismiss_button.isHidden()

    banner.show_success("All caught up.")
    assert not banner._dismiss_button.isHidden()


def test_banner_hides_its_action_button_with_no_action(qtbot):
    banner = DaemonStatusBanner()
    qtbot.addWidget(banner)

    banner.show_error("Something went wrong.")

    assert banner._action_button.isHidden()


def test_banner_shows_a_labelled_action_button_and_invokes_it(qtbot):
    banner = DaemonStatusBanner()
    qtbot.addWidget(banner)
    calls = []

    banner.show_error("Mostly done.", action=("Retry", lambda: calls.append("retry")))

    assert not banner._action_button.isHidden()
    assert banner._action_button.text() == "Retry"
    banner._action_button.click()
    assert calls == ["retry"]


def test_banner_action_button_is_replaced_not_stacked_across_calls(qtbot):
    """A second show_*(action=...) call must not leave the first
    callback also connected - otherwise clicking the button would fire
    both the stale and the new callback."""
    banner = DaemonStatusBanner()
    qtbot.addWidget(banner)
    calls = []

    banner.show_error("First.", action=("Retry", lambda: calls.append("first")))
    banner.show_error("Second.", action=("Retry", lambda: calls.append("second")))
    banner._action_button.click()

    assert calls == ["second"]


def test_banner_action_button_hides_again_once_a_later_call_has_none(qtbot):
    banner = DaemonStatusBanner()
    qtbot.addWidget(banner)

    banner.show_error("First.", action=("Retry", lambda: None))
    banner.show_info("Second.")

    assert banner._action_button.isHidden()


def test_banner_secondary_action_is_a_quiet_button_beside_the_primary(qtbot):
    banner = DaemonStatusBanner()
    qtbot.addWidget(banner)
    calls = []

    banner.show_error(
        "Something went wrong.",
        action=("Retry", lambda: calls.append("retry")),
        secondary_action=("Send feedback", lambda: calls.append("feedback")),
    )

    assert banner._secondary_button.text() == "Send feedback"
    assert banner._secondary_button.property("buttonKind") == "quiet"
    banner._secondary_button.click()
    banner._action_button.click()
    assert calls == ["feedback", "retry"]


def test_banner_secondary_action_is_replaced_not_stacked_and_hides_when_absent(qtbot):
    banner = DaemonStatusBanner()
    qtbot.addWidget(banner)
    calls = []

    banner.show_error("First.", secondary_action=("Send feedback", lambda: calls.append("first")))
    banner.show_error("Second.", secondary_action=("Send feedback", lambda: calls.append("second")))
    banner._secondary_button.click()
    banner.show_info("Third.")

    assert calls == ["second"]
    assert banner._secondary_button.isHidden()
