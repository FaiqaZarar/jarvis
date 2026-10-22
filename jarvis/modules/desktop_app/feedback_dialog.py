"""Feedback submission dialog (issue #145's Figma mockup: "Send feedback"
+ "Feedback sent" screens) - requires a signed-in cloud session, since
`POST /feedback` is session-gated (jarvis_cloud_server.server.api_feedback).

If no session exists yet, this dialog shows a short prompt to sign in
first rather than silently failing on submit - opening `SignInDialog`
directly from here and, on success, swapping straight into the feedback
form in place, so the user never has to go find Settings or reopen this
dialog themselves.
"""

from datetime import datetime, timezone
from typing import Optional

from PySide6.QtCore import Qt, QThreadPool
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QButtonGroup,
    QDialog,
    QDialogButtonBox,
    QFrame,
    QGraphicsDropShadowEffect,
    QHBoxLayout,
    QLabel,
    QLayout,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from jarvis.modules.cloud import client as cloud_client
from jarvis.modules.cloud import session_store
from jarvis.modules.desktop_app import log_excerpt, theme
from jarvis.modules.desktop_app.daemon_worker import run_daemon_call
from jarvis.modules.desktop_app.sign_in_dialog import SignInDialog

_SEVERITIES = (("minor", "Minor"), ("annoying", "Annoying"), ("blocking", "Blocking"))
_DEFAULT_SEVERITY = "annoying"


def _clear_layout(layout: QLayout) -> None:
    while layout.count():
        item = layout.takeAt(0)
        if item.widget():
            # hide() before deleteLater() - deletion is deferred to the
            # next event loop iteration, and without an explicit hide()
            # the old widget (e.g. the form's QTextEdit) can still be
            # visibly painted for a frame after being removed from the
            # layout, showing up as a stray styled box behind whatever
            # replaces it (confirmed via a real repro).
            item.widget().hide()
            item.widget().deleteLater()
        elif item.layout():
            _clear_layout(item.layout())


class FeedbackDialog(QDialog):
    def __init__(self, parent: Optional[QWidget], pool: QThreadPool):
        super().__init__(parent)
        self.setWindowTitle("Send feedback")
        self.setModal(True)
        self.resize(460, 420)
        self._pool = pool
        self._session = session_store.load_session()

        # No in-body heading repeating the window title ("Send
        # Feedback") - checked against every other dialog in this app
        # (ConnectClaudeDesktopDialog, SettingsDialog): none of them
        # restate their title bar text as a first body label, so doing
        # it here was inconsistent with this app's own convention, not
        # matching it.
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(24, 24, 24, 24)
        self._layout.setSpacing(theme.SPACING_LG)

        if self._session is None:
            self._build_sign_in_prompt()
        else:
            self._build_form()

    # -- sign-in prompt (no session yet) --------------------------------

    def _build_sign_in_prompt(self) -> None:
        self._layout.addWidget(QLabel("Sign in to send feedback - it's tied to your account."))
        sign_in_button = QPushButton("Sign in")
        sign_in_button.clicked.connect(self._on_sign_in_clicked)
        self._layout.addWidget(sign_in_button)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        buttons.rejected.connect(self.reject)
        self._layout.addWidget(buttons)

    def _on_sign_in_clicked(self) -> None:
        if not SignInDialog(self, self._pool, first_run=False).exec():
            return  # cancelled - stay on the sign-in prompt
        self._session = session_store.load_session()
        if self._session is None:
            return  # shouldn't happen (accept() only fires after save_session), but no session -> no form
        _clear_layout(self._layout)
        self._build_form()

    # -- feedback form ----------------------------------------------------

    def _build_form(self) -> None:
        self._layout.addWidget(QLabel("What's going on?"))
        self._body_field = QTextEdit()
        self._body_field.setAcceptRichText(False)
        self._body_field.setPlaceholderText(
            "Describe what happened - the more specific, the easier it is to act on."
        )
        self._layout.addWidget(self._body_field)

        self._layout.addWidget(QLabel("Severity"))
        severity_row = QHBoxLayout()
        self._severity_group = QButtonGroup(self)
        self._severity_by_button = {}
        for key, label in _SEVERITIES:
            button = QRadioButton(label)
            button.setChecked(key == _DEFAULT_SEVERITY)
            self._severity_group.addButton(button)
            self._severity_by_button[button] = key
            severity_row.addWidget(button)
        self._layout.addLayout(severity_row)

        self._status_label = QLabel("")
        self._status_label.setWordWrap(True)
        self._status_label.hide()
        self._layout.addWidget(self._status_label)

        # DESIGN.md §4.10: the main action on the right of Cancel. A
        # QDialogButtonBox puts it on the left on Windows.
        self._cancel_button = QPushButton("Cancel")
        self._cancel_button.clicked.connect(self.reject)
        self._send_button = QPushButton("Send feedback")
        self._send_button.setDefault(True)
        self._send_button.setProperty("buttonKind", "primary")
        self._send_button.clicked.connect(self._on_send_clicked)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        buttons.addWidget(self._cancel_button)
        buttons.addWidget(self._send_button)
        self._layout.addLayout(buttons)

    def _selected_severity(self) -> str:
        for button, key in self._severity_by_button.items():
            if button.isChecked():
                return key
        return _DEFAULT_SEVERITY

    def _on_send_clicked(self) -> None:
        body = self._body_field.toPlainText().strip()
        if not body:
            QMessageBox.warning(self, "Description required", "Describe what's going on.")
            return
        _email, session_token = self._session
        severity = self._selected_severity()

        # Captured right at the click, before the daemon call below ever
        # dispatches - log_excerpt.get_recent_log_excerpt needs a local
        # (naive) clock to compare against the log file's own local-time
        # asctime lines; client_timestamp is the UTC ISO string actually
        # sent/stored, a separate value for the same instant.
        click_time_local = datetime.now()
        client_timestamp = datetime.now(timezone.utc).isoformat()
        excerpt = log_excerpt.get_recent_log_excerpt(click_time_local)

        self._send_button.setEnabled(False)
        run_daemon_call(
            self._pool, cloud_client.submit_feedback, session_token, body, severity,
            client_timestamp, excerpt,
            on_success=self._on_submit_result, on_error=self._on_submit_failed,
        )

    def _on_submit_result(self, result: tuple) -> None:
        status, response_body = result
        if status == 200:
            self._show_confirmation()
            return
        if status == 401:
            session_store.clear_session()
            self._reset_after_failure("Your session expired - sign in again and retry.")
            return
        if status == 502:
            # The report itself is already saved server-side at this point
            # (see api_feedback) - only the GitHub issue creation failed.
            # Letting the user "retry" here would resubmit the same report
            # as a second, duplicate row - so this reads as done, with a
            # note, not as a retryable failure.
            self._show_confirmation(note=response_body.get("error"))
            return
        self._reset_after_failure(response_body.get("error", "Couldn't send feedback. Try again."))

    def _on_submit_failed(self, exc: Exception) -> None:
        self._reset_after_failure(str(exc))

    def _reset_after_failure(self, message: str) -> None:
        self._send_button.setEnabled(True)
        self._status_label.setText(message)
        self._status_label.setProperty("messageKind", "error")
        self._status_label.style().unpolish(self._status_label)
        self._status_label.style().polish(self._status_label)
        self._status_label.show()

    # -- confirmation (Figma's "Feedback sent" screen) -------------------

    def _show_confirmation(self, note: Optional[str] = None) -> None:
        _clear_layout(self._layout)
        # Deliberately not resizing the window here: _clear_layout()'s
        # deleteLater() calls are deferred, and resizing before Qt has
        # processed them (confirmed via a real repro) produced stale
        # content bleeding through from the window behind this one. The
        # dialog stays the form's fixed 460x420 in every state; the
        # confirmation content is one centered card (equal stretch
        # above/below) instead of pinned to the top with a dead gap.
        self._layout.addStretch(1)

        # A note (502 - the report was saved, but opening a GitHub issue
        # failed) is the only "not fully successful" case that reaches
        # here - a hard failure never gets this far (see _on_submit_result).
        is_partial = note is not None
        badge_color = theme.WARNING if is_partial else theme.SUCCESS
        badge_bg = theme.WARNING_BG if is_partial else theme.SUCCESS_BG

        card = QFrame()
        card.setObjectName("feedbackResultCard")
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(32, 32, 32, 32)
        card_layout.setSpacing(theme.SPACING_MD)

        # A colored circular badge holding the icon, not a bare glyph -
        # matches the reference design.
        badge = QLabel("!" if is_partial else "✓")
        badge.setFixedSize(64, 64)
        badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        badge_font = QFont()
        badge_font.setPixelSize(22)
        badge_font.setWeight(QFont.Weight.Bold)
        badge.setFont(badge_font)
        badge.setStyleSheet(
            f"background-color: {badge_bg}; color: {badge_color}; border-radius: 32px;"
        )
        badge_row = QHBoxLayout()
        badge_row.addStretch(1)
        badge_row.addWidget(badge)
        badge_row.addStretch(1)
        card_layout.addLayout(badge_row)

        # Heading and description as two separate lines (matching the
        # reference) instead of one run-on sentence - a bold neutral
        # heading (the badge already carries the color, the text
        # doesn't need to repeat it) plus a smaller secondary-color line
        # underneath, the same visual hierarchy this app already uses
        # elsewhere (e.g. a dialog's heading + explanatory banner text).
        heading_label = QLabel("Feedback saved" if is_partial else "Feedback sent")
        heading_font = QFont()
        heading_font.setPixelSize(theme.FONT_SIZE_HEADING)
        heading_font.setWeight(QFont.Weight.DemiBold)
        heading_label.setFont(heading_font)
        heading_label.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        # Explicit "background: transparent" - a child QLabel nested
        # inside a QSS-styled QFrame (feedbackResultCard's own background/
        # border/radius) doesn't automatically inherit a transparent
        # background from its parent; without this it painted its own
        # opaque default panel behind the text (confirmed via a real
        # repro - a visible box around each line of text).
        heading_label.setStyleSheet("background: transparent;")
        card_layout.addWidget(heading_label)

        # The raw GitHub issue URL is deliberately never shown here -
        # it's an internal tracking detail (which repo, which issue
        # number), not something the person submitting feedback needs
        # to act on.
        description_text = note or (
            "Thank you for your feedback. It has been recorded and will help "
            "inform future improvements."
        )
        description_label = QLabel(description_text)
        description_label.setWordWrap(True)
        description_label.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        description_label.setStyleSheet(f"background: transparent; color: {theme.TEXT_SECONDARY};")
        card_layout.addWidget(description_label)

        # "Send another" is a real action, not just a visual match to
        # the reference - it lets someone file a second, distinct report
        # without closing and reopening this dialog. It's always safe
        # here (unlike a "Try again" that would resubmit this exact
        # report) since the report behind this confirmation screen is
        # already saved either way - see _on_submit_result.
        send_another_button = QPushButton("Send another")
        send_another_button.clicked.connect(self._on_send_another_clicked)

        done_button = QPushButton("Done")
        done_button.setProperty("buttonKind", "primary")
        done_button.setDefault(True)
        done_button.clicked.connect(self.accept)
        # A soft accent-colored glow, matching the reference design -
        # QSS has no box-shadow equivalent, so this is a real
        # QGraphicsDropShadowEffect rather than a stylesheet rule.
        glow = QGraphicsDropShadowEffect(done_button)
        glow.setColor(QColor(theme.ACCENT))
        glow.setBlurRadius(24)
        glow.setOffset(0, 0)
        done_button.setGraphicsEffect(glow)

        button_row = QHBoxLayout()
        button_row.addStretch(1)
        button_row.addWidget(send_another_button)
        button_row.addWidget(done_button)
        button_row.addStretch(1)
        card_layout.addSpacing(theme.SPACING_SM)
        card_layout.addLayout(button_row)

        self._layout.addWidget(card)
        self._layout.addStretch(1)

    def _on_send_another_clicked(self) -> None:
        _clear_layout(self._layout)
        self._build_form()
