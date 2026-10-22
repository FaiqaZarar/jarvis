"""Cloud sign-in dialog (magic link) - mirrors settings_dialog.py's
first-run/revisit-anytime shape, but for the jarvis-cloud-server account
instead of the local embedding-provider config.

Required on first run when the build has a cloud server (so feedback can
be tied to an account): no Cancel button, and reject()/closeEvent() both
refuse to close the dialog before a session is saved - except that an
unreachable cloud service reveals a "Continue without signing in" escape,
so an outage (or a stale server URL) can't lock the user out of the whole
app, including Quit and updates. Reopened later
(from Settings, or the feedback prompt) it's ordinary and skippable via
Cancel - the gate is only ever `first_run=True`.

Callers that show this on first run must refresh anything that already
displays account state (settings_view.py's Account section is built
before this ever runs) - this dialog has no way to know who else is
showing stale state, so it can't refresh it itself.
"""

from typing import Optional

from PySide6.QtCore import QThreadPool, QTimer
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from jarvis.modules.cloud import client as cloud_client
from jarvis.modules.cloud import session_store
from jarvis.modules.desktop_app import theme
from jarvis.modules.desktop_app.daemon_worker import run_daemon_call

_POLL_INTERVAL_MS = 3000


class SignInDialog(QDialog):
    def __init__(self, parent: Optional[QWidget], pool: QThreadPool, *, first_run: bool = False):
        super().__init__(parent)
        self.setWindowTitle("Sign in to Jarvis")
        self.setModal(True)
        self.setMinimumWidth(440)
        self._pool = pool
        self._first_run = first_run
        # Set by _on_quit_clicked - the caller (main_window.py) checks
        # this after .exec() returns so it can stop right there instead
        # of continuing on to show the *next* first-run dialog (the
        # embedding-API-key settings screen) even though the user just
        # asked to quit (confirmed via a real repro: without this, Quit
        # looked like it did nothing, since a second modal popped up
        # immediately after this one closed).
        self.quit_requested = False
        self._request_id: Optional[str] = None
        self._email: Optional[str] = None
        # Guards against a slow round-trip (a retried request, or real
        # network latency to a real hosted server - unlike the local dev
        # setup this was built against) overlapping with the next tick,
        # which would fire a second concurrent poll. The server's atomic
        # delivery-claim already makes that safe (only one poll can ever
        # get the token), but the *other* one would then land on the
        # "already used elsewhere" branch and needlessly alarm the user
        # mid-success.
        self._poll_in_flight = False

        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(_POLL_INTERVAL_MS)
        self._poll_timer.timeout.connect(self._poll_status)

        # No in-body heading repeating the window title ("Sign in to
        # Jarvis") - none of this app's other dialogs restate their title
        # bar text as a first body label (checked: ConnectClaudeDesktopDialog,
        # SettingsDialog both go straight into content), so doing it here
        # read as a literal duplicate, not extra visual weight.
        banner_text = "We'll email you a one-time sign-in link."
        if first_run:
            banner_text += " Sign in to continue using Jarvis."
        self._banner = QLabel(banner_text)
        self._banner.setWordWrap(True)

        self._email_field = QLineEdit()
        self._email_field.setPlaceholderText("you@example.com")

        self._status_label = QLabel("")
        self._status_label.setWordWrap(True)
        self._status_label.hide()

        # First run has no Cancel/skip - every user is signed in so
        # feedback can be tied to an account, so this dialog is a hard
        # gate there (see reject()/closeEvent() below). Reopened later from
        # Settings or the feedback prompt, Cancel works as normal.
        self._send_button = QPushButton("Email me a sign-in link")
        self._send_button.setDefault(True)  # Enter in the email field submits
        self._send_button.setProperty("buttonKind", "primary")
        self._send_button.clicked.connect(self._on_send_clicked)
        # Shown once a link has been requested - the email can land in
        # spam or fail to arrive, and the only other way to get a new one
        # would be to close and reopen the dialog.
        self._resend_button = QPushButton("Resend link")
        self._resend_button.clicked.connect(self._on_send_clicked)
        self._resend_button.hide()
        # DESIGN.md §4.10: the main action on the right, Cancel beside it,
        # and Quit Jarvis (the first-run exit) apart on the left.
        buttons = QHBoxLayout()
        self._cancel_button: Optional[QPushButton] = None
        if not first_run:
            self._cancel_button = QPushButton("Cancel")
            self._cancel_button.clicked.connect(self._on_cancel)
        else:
            # The gate above leaves no way out, so a user who can't or
            # won't sign in (e.g. the link email never arrives) would be
            # stuck with a modal they can't dismiss and an app they can't
            # quit. Called through an attribute so tests can monkeypatch it
            # without quitting pytest-qt's shared QApplication.
            self._quit_app = QApplication.quit
            self._quit_button = QPushButton("Quit Jarvis")
            self._quit_button.setProperty("buttonKind", "quiet")
            self._quit_button.clicked.connect(self._on_quit_clicked)
            buttons.addWidget(self._quit_button)

        # Only shown once the cloud server proves unreachable (see
        # _on_request_failed) - never a way to skip sign-in while it works.
        self._skip_button = QPushButton("Continue without signing in")
        self._skip_button.setProperty("buttonKind", "quiet")
        self._skip_button.hide()
        self._skip_button.clicked.connect(self._on_skip)

        buttons.addStretch(1)
        buttons.addWidget(self._skip_button)
        if self._cancel_button is not None:
            buttons.addWidget(self._cancel_button)
        buttons.addWidget(self._resend_button)
        buttons.addWidget(self._send_button)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(theme.SPACING_LG)
        layout.addWidget(self._banner)
        layout.addWidget(self._email_field)
        layout.addWidget(self._status_label)
        layout.addSpacing(theme.SPACING_SM)
        layout.addLayout(buttons)

    def _set_status(self, message: str, kind: str) -> None:
        self._status_label.setText(message)
        self._status_label.setProperty("messageKind", kind)
        self._status_label.style().unpolish(self._status_label)
        self._status_label.style().polish(self._status_label)
        self._status_label.show()

    def _on_cancel(self) -> None:
        self._poll_timer.stop()
        self.reject()

    def _on_skip(self) -> None:
        self._first_run = False  # lift the gate; reject() below now closes
        self._on_cancel()

    def _on_quit_clicked(self) -> None:
        self._poll_timer.stop()
        self.quit_requested = True
        self._quit_app()
        # QApplication.quit() only takes effect once an event loop
        # actually processes it - it does *not* end this dialog's own
        # nested exec() loop, which reject() below is what's needed for
        # (confirmed via a real repro: without this, the dialog itself
        # never closed even though quit() had been called).
        self._first_run = False  # lift the gate so reject() below actually closes this dialog
        self.reject()

    def reject(self) -> None:
        if self._first_run:
            return  # required on first run - no Escape-key skip either
        super().reject()

    def closeEvent(self, event) -> None:
        if self._first_run:
            event.ignore()  # required on first run - no X-button skip either
            return
        super().closeEvent(event)

    def _on_send_clicked(self) -> None:
        email = self._email_field.text().strip()
        if not email or "@" not in email:
            QMessageBox.warning(self, "Email required", "Enter a valid email address.")
            return
        self._email = email
        self._send_button.setEnabled(False)
        self._resend_button.setEnabled(False)
        self._email_field.setEnabled(False)
        run_daemon_call(
            self._pool, cloud_client.request_magic_link, email,
            on_success=self._on_request_result, on_error=self._on_request_failed,
        )

    def _on_request_result(self, result: tuple) -> None:
        status, body = result
        if status != 200:
            self._reset_to_email_entry(body.get("error", "Couldn't send the link. Try again."))
            return
        self._request_id = body["request_id"]
        self._resend_button.show()
        self._resend_button.setEnabled(True)
        self._set_status(f"Check your email ({self._email}) and click the link to continue.", "info")
        self._poll_timer.start()

    def _on_request_failed(self, exc: Exception) -> None:
        self._reset_to_email_entry(str(exc))
        self._skip_button.show()

    def _reset_to_email_entry(self, message: str) -> None:
        self._poll_timer.stop()
        self._resend_button.hide()
        self._send_button.setEnabled(True)
        self._email_field.setEnabled(True)
        self._set_status(message, "error")

    def _poll_status(self) -> None:
        if self._request_id is None or self._poll_in_flight:
            return
        self._poll_in_flight = True
        run_daemon_call(
            self._pool, cloud_client.poll_login_status, self._request_id,
            on_success=self._on_poll_result, on_error=self._on_poll_failed,
        )

    def _on_poll_result(self, result: tuple) -> None:
        self._poll_in_flight = False
        status, body = result
        if status != 200 or body.get("status") != "confirmed":
            return  # still pending - the timer just fires again
        session_token = body.get("session_token")
        if not session_token:
            # Already confirmed but this dialog didn't win the one-time
            # delivery (e.g. a previous instance already claimed it) -
            # nothing more this dialog can do; let the user retry.
            self._poll_timer.stop()
            self._send_button.setEnabled(True)
            self._email_field.setEnabled(True)
            self._set_status(
                "This link was already used to sign in elsewhere. Request a new one.", "error",
            )
            return
        self._poll_timer.stop()
        session_store.save_session(self._email, session_token)
        self.accept()

    def _on_poll_failed(self, exc: Exception) -> None:
        self._poll_in_flight = False
        # A transient poll failure isn't fatal - the timer just tries
        # again in _POLL_INTERVAL_MS. Only surface it if it keeps
        # happening would need a failure-count threshold; not worth the
        # complexity for a background poll the user isn't watching
        # closely (they're in their email client, not this dialog).
        pass
