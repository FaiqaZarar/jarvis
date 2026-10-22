"""Cloud sign-in dialog (jarvis.modules.desktop_app.sign_in_dialog). Real
`run_daemon_call` -> `QThreadPool` path throughout, not a synchronous
shortcut - `cloud_client.request_magic_link`/`poll_login_status` are the
only things mocked, matching the same real-threaded-path convention
`test_desktop_app_views.py`'s daemon-call tests already use.
"""

from PySide6.QtCore import QThreadPool
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import QPushButton

from jarvis.modules.cloud import client as cloud_client
from jarvis.modules.cloud import session_store
from jarvis.modules.desktop_app.sign_in_dialog import SignInDialog


def _dialog(qtbot) -> SignInDialog:
    dialog = SignInDialog(None, QThreadPool())
    qtbot.addWidget(dialog)
    return dialog


def test_requires_a_valid_email(qtbot, monkeypatch):
    monkeypatch.setattr(
        "jarvis.modules.desktop_app.sign_in_dialog.QMessageBox.warning",
        staticmethod(lambda *a, **k: None),
    )
    calls = []
    monkeypatch.setattr(cloud_client, "request_magic_link", lambda email: calls.append(email))
    dialog = _dialog(qtbot)

    dialog._email_field.setText("not-an-email")
    dialog._on_send_clicked()

    assert calls == []  # never dispatched - failed client-side validation


def test_sends_request_and_shows_check_email_status(qtbot, monkeypatch):
    monkeypatch.setattr(
        cloud_client, "request_magic_link",
        lambda email: (200, {"request_id": "req-1"}),
    )
    dialog = _dialog(qtbot)
    dialog._email_field.setText("user@example.com")

    dialog._on_send_clicked()

    qtbot.waitUntil(lambda: dialog._request_id == "req-1", timeout=2000)
    assert "check your email" in dialog._status_label.text().lower()
    assert dialog._status_label.property("messageKind") == "info"
    assert not dialog._send_button.isEnabled()  # stays disabled while polling
    assert dialog._poll_timer.isActive()


def test_shows_an_error_and_re_enables_the_form_on_a_failed_request(qtbot, monkeypatch):
    monkeypatch.setattr(
        cloud_client, "request_magic_link",
        lambda email: (429, {"error": "Too many login attempts for this email - try again later."}),
    )
    dialog = _dialog(qtbot)
    dialog._email_field.setText("user@example.com")

    dialog._on_send_clicked()

    qtbot.waitUntil(lambda: dialog._send_button.isEnabled(), timeout=2000)
    assert "too many" in dialog._status_label.text().lower()
    assert dialog._status_label.property("messageKind") == "error"
    assert dialog._email_field.isEnabled()


def test_poll_in_flight_guard_prevents_a_second_overlapping_poll(qtbot, monkeypatch):
    calls = []
    monkeypatch.setattr(
        cloud_client, "poll_login_status",
        lambda request_id: calls.append(request_id) or (200, {"status": "pending"}),
    )
    dialog = _dialog(qtbot)
    dialog._request_id = "req-1"
    dialog._poll_in_flight = True  # simulate a poll already in flight

    dialog._poll_status()

    assert calls == []  # the guard skipped dispatching a second one


def test_poll_confirmed_with_token_saves_session_and_accepts(qtbot, monkeypatch):
    monkeypatch.setattr(
        cloud_client, "poll_login_status",
        lambda request_id: (200, {"status": "confirmed", "session_token": "tok-123"}),
    )
    saved = {}
    monkeypatch.setattr(session_store, "save_session", lambda email, token: saved.update(email=email, token=token))
    dialog = _dialog(qtbot)
    dialog._request_id = "req-1"
    dialog._email = "user@example.com"

    dialog._poll_status()

    qtbot.waitUntil(lambda: dialog.result() == SignInDialog.DialogCode.Accepted, timeout=2000)
    assert saved == {"email": "user@example.com", "token": "tok-123"}
    assert not dialog._poll_timer.isActive()


def test_poll_confirmed_without_token_shows_already_used_message(qtbot, monkeypatch):
    monkeypatch.setattr(
        cloud_client, "poll_login_status",
        lambda request_id: (200, {"status": "confirmed"}),  # no session_token - lost the delivery race
    )
    dialog = _dialog(qtbot)
    dialog._request_id = "req-1"
    dialog._email = "user@example.com"
    dialog._poll_timer.start()

    dialog._poll_status()

    qtbot.waitUntil(lambda: not dialog._poll_timer.isActive(), timeout=2000)
    assert "already used" in dialog._status_label.text().lower()
    assert dialog._status_label.property("messageKind") == "error"
    assert dialog._send_button.isEnabled()


def test_cancel_stops_polling_and_rejects(qtbot):
    dialog = _dialog(qtbot)
    dialog._request_id = "req-1"
    dialog._poll_timer.start()

    dialog._on_cancel()

    assert not dialog._poll_timer.isActive()
    assert dialog.result() == SignInDialog.DialogCode.Rejected


def test_first_run_has_no_cancel_button_and_cannot_be_rejected(qtbot, monkeypatch):
    """Regression test: first-run sign-in is now required (every user is
    every user signed in so feedback can be tied to an account) - Escape,
    the X button, and any programmatic reject() must all be no-ops until
    a session is actually saved."""
    dialog = SignInDialog(None, QThreadPool(), first_run=True)
    qtbot.addWidget(dialog)

    assert dialog._cancel_button is None
    assert "Cancel" not in {b.text() for b in dialog.findChildren(QPushButton)}

    done_calls = []
    monkeypatch.setattr(dialog, "done", lambda code: done_calls.append(code))

    dialog.reject()
    assert done_calls == []  # QDialog.reject()'s done(Rejected) never ran

    event = QCloseEvent()
    dialog.closeEvent(event)
    assert not event.isAccepted()
    assert done_calls == []


def test_resend_link_requests_a_fresh_link_and_switches_to_its_request_id(qtbot, monkeypatch):
    ids = iter(["req-1", "req-2"])
    monkeypatch.setattr(
        cloud_client, "request_magic_link",
        lambda email: (200, {"request_id": next(ids)}),
    )
    dialog = _dialog(qtbot)
    dialog._email_field.setText("user@example.com")
    dialog._on_send_clicked()
    qtbot.waitUntil(lambda: dialog._request_id == "req-1", timeout=2000)
    assert not dialog._resend_button.isHidden()

    dialog._resend_button.click()

    qtbot.waitUntil(lambda: dialog._request_id == "req-2", timeout=2000)
    assert dialog._resend_button.isEnabled()


def test_failed_resend_shows_the_server_error_and_returns_to_email_entry(qtbot, monkeypatch):
    results = iter([(200, {"request_id": "req-1"}), (502, {"error": "Couldn't send the sign-in email: boom"})])
    monkeypatch.setattr(cloud_client, "request_magic_link", lambda email: next(results))
    dialog = _dialog(qtbot)
    dialog._email_field.setText("user@example.com")
    dialog._on_send_clicked()
    qtbot.waitUntil(lambda: dialog._request_id == "req-1", timeout=2000)

    dialog._resend_button.click()

    qtbot.waitUntil(lambda: dialog._send_button.isEnabled(), timeout=2000)
    assert "boom" in dialog._status_label.text()
    assert dialog._resend_button.isHidden()
    assert not dialog._poll_timer.isActive()


def test_first_run_has_a_quit_button_that_quits_the_app(qtbot, monkeypatch):
    """Regression test: clicking Quit must both call QApplication.quit()
    AND actually close this dialog's own exec() loop - quit() alone
    doesn't end a nested QDialog.exec() loop, so without also calling
    reject() here, the dialog (and the whole app, since main_window.py
    was still waiting on this .exec() to return) never actually closed,
    even though quit() had fired."""
    dialog = SignInDialog(None, QThreadPool(), first_run=True)
    qtbot.addWidget(dialog)
    quits = []
    dialog._quit_app = lambda: quits.append(True)

    dialog._quit_button.click()

    assert quits == [True]
    assert not dialog._poll_timer.isActive()
    assert dialog.quit_requested is True
    assert dialog.result() == SignInDialog.DialogCode.Rejected


def test_send_button_is_plain_words_and_the_main_action_sits_rightmost(qtbot):
    dialog = SignInDialog(None, QThreadPool(), first_run=False)
    qtbot.addWidget(dialog)
    dialog.show()
    qtbot.waitExposed(dialog)

    assert dialog._send_button.text() == "Email me a sign-in link"
    assert dialog._send_button.geometry().left() > dialog._cancel_button.geometry().right()


def test_no_large_gap_above_the_buttons(qtbot):
    dialog = SignInDialog(None, QThreadPool(), first_run=True)
    qtbot.addWidget(dialog)
    dialog.show()
    qtbot.waitExposed(dialog)

    gap = dialog._send_button.geometry().top() - dialog._email_field.geometry().bottom()
    assert gap <= 48
