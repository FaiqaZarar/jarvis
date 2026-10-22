"""Feedback submission dialog (jarvis.modules.desktop_app.feedback_dialog).
Real `run_daemon_call` -> `QThreadPool` path throughout - only
`cloud_client.submit_feedback` and `session_store` are mocked, matching
test_desktop_app_sign_in.py's own convention.
"""

from PySide6.QtCore import QThreadPool

from jarvis.modules.cloud import client as cloud_client
from jarvis.modules.cloud import session_store
from jarvis.modules.desktop_app.feedback_dialog import FeedbackDialog
from jarvis.modules.desktop_app.sign_in_dialog import SignInDialog


def _signed_in(monkeypatch):
    monkeypatch.setattr(session_store, "load_session", lambda: ("user@example.com", "tok-abc"))


def _dialog(qtbot) -> FeedbackDialog:
    dialog = FeedbackDialog(None, QThreadPool())
    qtbot.addWidget(dialog)
    return dialog


def _layout_texts(layout) -> list:
    """Every widget's `.text()` reachable from `layout`, recursing into
    nested layouts/widgets (the confirmation screen's content lives
    inside a card QFrame, not as direct children of the dialog's own
    layout) - used instead of a fixed `itemAt(N)` index so these tests
    don't need updating every time this screen's layout is rebalanced."""
    texts = []
    for i in range(layout.count()):
        item = layout.itemAt(i)
        widget = item.widget()
        if widget is not None:
            if hasattr(widget, "text"):
                texts.append(widget.text())
            if widget.layout() is not None:
                texts.extend(_layout_texts(widget.layout()))
        elif item.layout() is not None:
            texts.extend(_layout_texts(item.layout()))
    return texts


def test_shows_sign_in_prompt_when_no_session(qtbot, monkeypatch):
    monkeypatch.setattr(session_store, "load_session", lambda: None)

    dialog = _dialog(qtbot)

    assert not hasattr(dialog, "_body_field")


def test_shows_the_form_directly_when_already_signed_in(qtbot, monkeypatch):
    _signed_in(monkeypatch)

    dialog = _dialog(qtbot)

    assert hasattr(dialog, "_body_field")
    assert dialog._selected_severity() == "annoying"  # the default


def test_sign_in_from_the_prompt_swaps_straight_into_the_form(qtbot, monkeypatch):
    monkeypatch.setattr(session_store, "load_session", lambda: None)
    dialog = _dialog(qtbot)
    assert not hasattr(dialog, "_body_field")

    class _FakeSignInDialog:
        def __init__(self, parent, pool, *, first_run=False):
            pass

        def exec(self):
            monkeypatch.setattr(session_store, "load_session", lambda: ("user@example.com", "tok-abc"))
            return 1  # Accepted

    import jarvis.modules.desktop_app.feedback_dialog as feedback_dialog_mod
    monkeypatch.setattr(feedback_dialog_mod, "SignInDialog", _FakeSignInDialog)

    dialog._on_sign_in_clicked()

    assert hasattr(dialog, "_body_field")


def test_rejects_an_empty_description(qtbot, monkeypatch):
    monkeypatch.setattr(
        "jarvis.modules.desktop_app.feedback_dialog.QMessageBox.warning",
        staticmethod(lambda *a, **k: None),
    )
    _signed_in(monkeypatch)
    calls = []
    monkeypatch.setattr(cloud_client, "submit_feedback", lambda *a: calls.append(a))
    dialog = _dialog(qtbot)

    dialog._body_field.setPlainText("   ")
    dialog._on_send_clicked()

    assert calls == []


def test_successful_submission_shows_the_confirmation_card(qtbot, monkeypatch):
    """Regression test: the raw GitHub issue URL is deliberately NOT
    shown to the person submitting feedback - it's an internal tracking
    detail, not something they need to act on (see _show_confirmation's
    docstring)."""
    _signed_in(monkeypatch)
    monkeypatch.setattr(
        cloud_client, "submit_feedback",
        lambda token, body, severity, client_timestamp, log_excerpt: (
            200, {"issue_url": "https://github.com/example/jarvis/issues/999"},
        ),
    )
    dialog = _dialog(qtbot)
    dialog._body_field.setPlainText("search is slow")

    dialog._on_send_clicked()

    qtbot.waitUntil(
        lambda: any("Feedback sent" in t for t in _layout_texts(dialog._layout)), timeout=2000,
    )
    assert not any("999" in t for t in _layout_texts(dialog._layout))


def test_send_another_returns_to_a_blank_form(qtbot, monkeypatch):
    _signed_in(monkeypatch)
    monkeypatch.setattr(
        cloud_client, "submit_feedback",
        lambda token, body, severity, client_timestamp, log_excerpt: (200, {"issue_url": None}),
    )
    dialog = _dialog(qtbot)
    dialog._body_field.setPlainText("search is slow")
    dialog._on_send_clicked()
    qtbot.waitUntil(
        lambda: any("Feedback sent" in t for t in _layout_texts(dialog._layout)), timeout=2000,
    )

    dialog._on_send_another_clicked()

    assert hasattr(dialog, "_body_field")
    assert dialog._body_field.toPlainText() == ""


def test_a_502_shows_confirmation_instead_of_letting_the_user_resubmit(qtbot, monkeypatch):
    """Regression test: the report is already saved server-side on a 502
    (only the GitHub issue creation failed) - resubmitting would create a
    duplicate row, so this must land on the confirmation screen, not
    silently reopen the form for a retry."""
    _signed_in(monkeypatch)
    submit_calls = []
    monkeypatch.setattr(
        cloud_client, "submit_feedback",
        lambda token, body, severity, client_timestamp, log_excerpt: submit_calls.append(1) or (
            502, {"error": "Feedback was saved, but opening a GitHub issue failed."},
        ),
    )
    dialog = _dialog(qtbot)
    dialog._body_field.setPlainText("search is slow")

    dialog._on_send_clicked()

    qtbot.waitUntil(
        lambda: any("Feedback saved" in t for t in _layout_texts(dialog._layout)), timeout=2000,
    )
    assert len(submit_calls) == 1  # landing here doesn't auto-resubmit the same report


def test_a_401_clears_the_session_and_keeps_the_form_for_a_retry(qtbot, monkeypatch):
    _signed_in(monkeypatch)
    monkeypatch.setattr(
        cloud_client, "submit_feedback",
        lambda token, body, severity, client_timestamp, log_excerpt: (401, {"error": "Sign in again"}),
    )
    cleared = []
    monkeypatch.setattr(session_store, "clear_session", lambda: cleared.append(1))
    dialog = _dialog(qtbot)
    dialog._body_field.setPlainText("search is slow")

    dialog._on_send_clicked()

    qtbot.waitUntil(lambda: dialog._send_button.isEnabled(), timeout=2000)
    assert cleared == [1]
    assert hasattr(dialog, "_body_field")  # still the form, not confirmation
    assert dialog._status_label.property("messageKind") == "error"


def test_severity_selection_is_sent_with_the_report(qtbot, monkeypatch):
    _signed_in(monkeypatch)
    captured = {}
    monkeypatch.setattr(
        cloud_client, "submit_feedback",
        lambda token, body, severity, client_timestamp, log_excerpt: captured.update(
            severity=severity,
        ) or (200, {"issue_url": None}),
    )
    dialog = _dialog(qtbot)
    dialog._body_field.setPlainText("search is slow")
    for button, key in dialog._severity_by_button.items():
        if key == "blocking":
            button.setChecked(True)

    dialog._on_send_clicked()

    qtbot.waitUntil(lambda: "severity" in captured, timeout=2000)
    assert captured["severity"] == "blocking"


def test_sends_a_client_timestamp_and_log_excerpt_with_the_report(qtbot, monkeypatch):
    """Regression test: a feedback report must carry enough debugging
    context on its own that a vague "it's not working" is actionable -
    see jarvis.modules.desktop_app.log_excerpt's own docstring for why."""
    _signed_in(monkeypatch)
    import jarvis.modules.desktop_app.feedback_dialog as feedback_dialog_mod
    monkeypatch.setattr(
        feedback_dialog_mod.log_excerpt, "get_recent_log_excerpt", lambda before: "INFO: something happened",
    )
    captured = {}
    monkeypatch.setattr(
        cloud_client, "submit_feedback",
        lambda token, body, severity, client_timestamp, log_excerpt: captured.update(
            client_timestamp=client_timestamp, log_excerpt=log_excerpt,
        ) or (200, {"issue_url": None}),
    )
    dialog = _dialog(qtbot)
    dialog._body_field.setPlainText("search is slow")

    dialog._on_send_clicked()

    qtbot.waitUntil(lambda: "client_timestamp" in captured, timeout=2000)
    assert captured["client_timestamp"]  # non-empty ISO timestamp
    assert captured["log_excerpt"] == "INFO: something happened"


def test_send_feedback_sits_right_of_cancel(qtbot, monkeypatch):
    _signed_in(monkeypatch)
    dialog = _dialog(qtbot)
    dialog.show()
    qtbot.waitExposed(dialog)

    assert dialog.windowTitle() == "Send feedback"
    assert dialog._send_button.geometry().left() > dialog._cancel_button.geometry().right()
