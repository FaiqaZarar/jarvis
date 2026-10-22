"""Daemon-error presentation for the GUI (design doc §5), plus the
general-purpose status banner the redesign reuses for live ingest
feedback (docs/desktop-app-ux-redesign-DESIGN.md §4/§6).

Reuses the exact three exception types and their pre-written messages
from `jarvis.modules.daemon.client` - `DaemonStartupError` (tier 1),
`DaemonConnectionError` (tier 2), `DaemonTimeoutError` (tier 3) - already
built, already tested, already carrying a calm, user-facing message.
Nothing here re-classifies a failure or invents a new message; the only
new thing a GUI needs beyond what the CLI/MCP proxy already had is
*which widget* shows that message:

  - Read actions (search/browse) and the startup daemon check: a
    non-blocking banner (`DaemonStatusBanner`) - the rest of the UI
    stays usable, the user can dismiss it and keep going.
  - Write actions (ingest): the same banner, not a modal (DESIGN.md
    §4.10, §5.4: failures never appear in a modal). `ConnectorsView`
    uses one whose errors can't be dismissed (`errors_dismissible=False`)
    - an error banner stays until resolved (§4.2), so a failed sync
    can't be lost by closing it.

`DaemonStatusBanner` no longer hardcodes a color in Python
(`setStyleSheet("color: #7a1f1f;")`, the one inline style anywhere in
this app before `theme.py` existed) - `show_error`/`show_info`/
`show_success` set a `bannerKind` dynamic property instead, styled by
`theme.py`'s QSS attribute selectors, and fade in/out via
`QPropertyAnimation` rather than an abrupt `show()`/`hide()`.
"""

import html
from typing import Callable, Optional

from PySide6.QtCore import Qt, QPropertyAnimation
from PySide6.QtWidgets import (
    QDialog, QGraphicsOpacityEffect, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget,
)

from jarvis.modules.desktop_app import theme

_FADE_DURATION_MS = 180

_Action = tuple[str, Callable[[], None]]

_SENTENCE_ENDS = ".?…"


def _split_lead(message: str) -> tuple[str, str]:
    """`(first sentence, the rest)`. A full stop inside “…” is part of a
    quoted name ("Syncing “Q3. Final”."), not the end of the sentence."""
    in_quote = False
    for i, ch in enumerate(message):
        if ch == "“":
            in_quote = True
        elif ch == "”":
            in_quote = False
        elif (
            ch in _SENTENCE_ENDS and not in_quote
            and i + 1 < len(message) and message[i + 1].isspace()
        ):
            return message[:i + 1], message[i + 1:].lstrip()
    return message, ""


def banner_markup(message: str) -> str:
    """`message` as rich text with its first sentence bold - DESIGN.md
    §4.2's "a bold lead, one sentence". A message of one sentence is all
    lead."""
    lead, rest = _split_lead(message)
    markup = f"<span style='font-weight:600'>{html.escape(lead)}</span>"
    return f"{markup} {html.escape(rest)}" if rest else markup


def banner_layout(widget: QWidget) -> QHBoxLayout:
    layout = QHBoxLayout(widget)
    layout.setContentsMargins(theme.SPACING_LG, theme.SPACING_MD, theme.SPACING_LG, theme.SPACING_MD)
    layout.setSpacing(theme.SPACING_SM)
    return layout


def _bind_button(button: QPushButton, connected: bool, action: Optional[_Action]) -> bool:
    """Point `button` at `action` (or hide it), dropping its previous
    callback first. Returns whether it is now connected."""
    if connected:
        button.clicked.disconnect()
    if action is None:
        button.hide()
        return False
    label, callback = action
    button.setText(label)
    button.clicked.connect(callback)
    button.show()
    return True


class DaemonStatusBanner(QWidget):
    """Non-blocking inline banner. Hidden by default; `show_error`/
    `show_info`/`show_success` fade it in with the given message and
    kind, `dismiss()` (the Dismiss button, or a caller done showing it)
    fades it back out and hides it once the fade completes.

    `errors_dismissible=False` keeps Dismiss off every error this banner
    shows, while info and success stay dismissible (DESIGN.md §4.2) - for
    a banner whose errors only go away once resolved (ConnectorsView's
    sync failures).

    Each `show_*` call optionally takes `action=(label, callback)` - one
    extra button shown before Dismiss (DESIGN.md §6 - most failure/
    syncing/setup messages pair with exactly one action, e.g. Retry or
    Restart service) - and `secondary_action`, a quiet button beside it
    (DESIGN.md §4.2; e.g. "Send feedback" next to Retry). Not a list:
    DESIGN.md's own principle 2 is one primary action per state, not
    several competing for attention.
    """

    def __init__(self, parent: QWidget = None, errors_dismissible: bool = True):
        super().__init__(parent)
        # A plain QWidget subclass otherwise never paints its own QSS
        # background/border/radius at all (DESIGN.md §8 - confirmed a
        # real gap, not hypothetical) - this is the standard Qt fix, not
        # a custom paintEvent.
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._errors_dismissible = errors_dismissible
        self._message = ""
        self._label = QLabel()
        self._label.setWordWrap(True)
        self._label.setTextFormat(Qt.TextFormat.RichText)
        self._label.setObjectName("bannerLabel")

        self._action_button = QPushButton()
        self._action_button.hide()
        self._action_connected = False
        # DESIGN.md §4.2: "at most one primary action, an optional quiet
        # secondary action" - e.g. Retry plus Send feedback.
        self._secondary_button = QPushButton()
        self._secondary_button.setProperty("buttonKind", "quiet")
        self._secondary_button.hide()
        self._secondary_connected = False

        self._dismiss_button = QPushButton("Dismiss")
        self._dismiss_button.setProperty("buttonKind", "quiet")
        self._dismiss_button.clicked.connect(self.dismiss)

        layout = banner_layout(self)
        layout.addWidget(self._label, 1)
        layout.addWidget(self._secondary_button)
        layout.addWidget(self._action_button)
        layout.addWidget(self._dismiss_button)

        self._opacity = QGraphicsOpacityEffect(self)
        self._opacity.setOpacity(0.0)
        self.setGraphicsEffect(self._opacity)

        self._fade_in_anim = QPropertyAnimation(self._opacity, b"opacity", self)
        self._fade_in_anim.setDuration(_FADE_DURATION_MS)
        self._fade_in_anim.setEndValue(1.0)

        self._fade_out_anim = QPropertyAnimation(self._opacity, b"opacity", self)
        self._fade_out_anim.setDuration(_FADE_DURATION_MS)
        self._fade_out_anim.setEndValue(0.0)
        # Connected once, here - not per-dismiss() call, which would
        # stack a fresh connection (and a fresh hide()) on every call.
        self._fade_out_anim.finished.connect(self.hide)

        self.hide()

    def show_error(
        self, message: str, action: Optional[_Action] = None, secondary_action: Optional[_Action] = None,
        *, dismissible: bool = True,
    ) -> None:
        """`dismissible=False` hides Dismiss for this message only, for an
        error that must stay until it's resolved (DESIGN.md §4.2) on a
        banner that otherwise shows passing ones too."""
        self._show(message, "error", action, secondary_action, dismissible)

    def show_info(
        self, message: str, action: Optional[_Action] = None, secondary_action: Optional[_Action] = None,
    ) -> None:
        self._show(message, "info", action, secondary_action)

    def show_success(
        self, message: str, action: Optional[_Action] = None, secondary_action: Optional[_Action] = None,
    ) -> None:
        self._show(message, "success", action, secondary_action)

    def _show(
        self, message: str, kind: str, action: Optional[_Action], secondary_action: Optional[_Action],
        dismissible: bool = True,
    ) -> None:
        self._fade_out_anim.stop()
        self._dismiss_button.setVisible(dismissible and (kind != "error" or self._errors_dismissible))
        self._message = message
        self._label.setText(banner_markup(message))
        self._action_connected = _bind_button(self._action_button, self._action_connected, action)
        self._secondary_connected = _bind_button(
            self._secondary_button, self._secondary_connected, secondary_action,
        )
        for widget in (self, self._label):
            widget.setProperty("bannerKind", kind)
            widget.style().unpolish(widget)
            widget.style().polish(widget)
        self.show()
        self._fade_in_anim.stop()
        self._fade_in_anim.setStartValue(self._opacity.opacity())
        self._fade_in_anim.start()

    def message(self) -> str:
        return self._message

    def dismiss(self) -> None:
        if self.isHidden():
            return
        self._fade_in_anim.stop()
        self._fade_out_anim.stop()
        self._fade_out_anim.setStartValue(self._opacity.opacity())
        self._fade_out_anim.start()


class ConfirmDialog(QDialog):
    """DESIGN.md §4.10's confirm dialog: the title as a bold heading, the
    consequence beneath it, Cancel then the action on the right, focus
    starting on Cancel. Built by hand rather than as a `QMessageBox`,
    whose Windows button order puts the action on the left and whose
    buttons render with native chrome instead of the theme."""

    def __init__(self, parent, title: str, message: str, action_label: str, *, destructive: bool = False):
        super().__init__(parent)
        self.setWindowTitle(title)

        heading = QLabel(title)
        heading.setObjectName("dialogTitle")
        heading.setWordWrap(True)
        body = QLabel(message)
        body.setObjectName("confirmDialogMessage")
        body.setWordWrap(True)

        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.clicked.connect(self.reject)
        self.action_button = QPushButton(action_label)
        self.action_button.setProperty("buttonKind", "destructive" if destructive else "primary")
        self.action_button.setAutoDefault(False)
        self.action_button.clicked.connect(self.accept)
        self.cancel_button.setDefault(True)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        buttons.addWidget(self.cancel_button)
        buttons.addWidget(self.action_button)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(theme.SPACING_XL, theme.SPACING_XL, theme.SPACING_XL, theme.SPACING_LG)
        layout.setSpacing(theme.SPACING_SM)
        layout.addWidget(heading)
        layout.addWidget(body)
        layout.addSpacing(theme.SPACING_LG)
        layout.addLayout(buttons)
        self.setMinimumWidth(400)
        self.cancel_button.setFocus()


def confirm_action(parent, title: str, message: str, action_label: str, *, destructive: bool = False) -> bool:
    """Ask before an action (DESIGN.md §4.10): the title states the
    action, the message its consequence, and the buttons name it a third
    time rather than "Yes". `destructive=True` colours the action button
    with the ERROR token. Returns `True` only when the action was chosen."""
    return ConfirmDialog(parent, title, message, action_label, destructive=destructive).exec() == QDialog.DialogCode.Accepted
