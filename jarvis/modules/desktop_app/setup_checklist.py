"""The setup checklist widgets (DESIGN.md §4.7): a full card (the empty
Sources page's main content) and a compact strip (shown above the
sources list once partly done), both built from the same row model and
`itemState` dynamic-property convention `theme.py` already uses for
banners/steps elsewhere - no new styling convention invented here.

Also owns the one piece of state that needs to persist across restarts:
whether the user dismissed the (optional) Claude Desktop step. Lives
here, not in `setup_health.py`, because `QSettings` is a Qt class and
that module is required to stay Qt-free for pure testability.
"""

import dataclasses
import html
from dataclasses import dataclass
from typing import Callable, Optional

from PySide6.QtCore import QSettings, Qt
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton, QSizePolicy, QVBoxLayout, QWidget

from jarvis.modules.desktop_app import theme
from jarvis.modules.desktop_app.setup_health import ItemState

_DISMISS_SETTINGS_KEY = "SetupChecklist/claudeDesktopDismissed"

_ITEM_ICON = {
    "done": (theme.CHECK_CIRCLE_SVG, theme.SUCCESS),
    "to_do": (theme.EMPTY_CIRCLE_SVG, theme.TEXT_SECONDARY),
    "needs_attention": (theme.ALERT_CIRCLE_SVG, theme.WARNING),
    "checking": (theme.CLOCK_SVG, theme.TEXT_DISABLED),
}
_ICON_SIZE = 16
# Wide enough for a row's sentence and both of its buttons on one line,
# narrow enough that the card reads as one block, not a page-wide band.
CARD_MAX_WIDTH = 640


def _settings() -> QSettings:
    # A seam, not an inline QSettings() call - mirrors nav_rail.py's own
    # _settings() exactly (duplicated, not imported/shared - this
    # codebase's established per-module convention).
    return QSettings()


def is_claude_desktop_dismissed() -> bool:
    return _settings().value(_DISMISS_SETTINGS_KEY, False, type=bool)


def set_claude_desktop_dismissed(dismissed: bool) -> None:
    _settings().setValue(_DISMISS_SETTINGS_KEY, dismissed)


@dataclass
class ChecklistItem:
    """`strip_text` is the sentence the compact strip uses when this is
    the next step (DESIGN.md §6: "Connect Claude Desktop so Claude can use
    your files."), `label` when unset. `emphasized` makes the primary
    action the page's one primary button (§4.3)."""
    label: str
    state: ItemState
    message: Optional[str] = None
    primary_action: Optional[tuple[str, Callable[[], None]]] = None
    secondary_action: Optional[tuple[str, Callable[[], None]]] = None
    strip_text: Optional[str] = None
    emphasized: bool = False


class _ChecklistRow(QFrame):
    """One row, shared by both variants. A `dismissed` item is hidden
    entirely (DESIGN.md: dismissed items are "hidden"), not styled-but-
    visible. `markup`, when given, replaces the label with rich text (the
    strip's summary sentence)."""

    def __init__(self, item: ChecklistItem, parent: QWidget = None, markup: Optional[str] = None):
        super().__init__(parent)
        self.setObjectName("setupChecklistRow")
        self.setVisible(item.state != "dismissed")

        self._icon = QLabel()
        self._icon.setObjectName("setupChecklistRowIcon")
        self._icon.setFixedSize(_ICON_SIZE, _ICON_SIZE)
        if item.state in _ITEM_ICON:
            svg, color = _ITEM_ICON[item.state]
            # Rendered at 2x so high-density screens get a sharp icon.
            self._icon.setPixmap(theme.icon_from_svg(svg, color, _ICON_SIZE * 2).pixmap(_ICON_SIZE, _ICON_SIZE))

        self._label = QLabel()
        if markup is None:
            self._label.setText(item.label)
        else:
            self._label.setTextFormat(Qt.TextFormat.RichText)
            self._label.setText(markup)
        self._label.setObjectName("setupChecklistRowLabel")
        self._label.setProperty("itemState", item.state)
        # Ignored width lets a narrow window wrap the sentence instead of
        # the buttons pushing the row past the card's edge.
        self._label.setWordWrap(True)
        self._label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)

        text_col = QVBoxLayout()
        text_col.setContentsMargins(0, 0, 0, 0)
        text_col.setSpacing(2)
        text_col.addWidget(self._label)
        if item.message:
            message_label = QLabel(item.message)
            message_label.setObjectName("setupChecklistRowMessage")
            message_label.setWordWrap(True)
            message_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
            text_col.addWidget(message_label)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(theme.SPACING_SM)
        layout.addWidget(self._icon)
        layout.addLayout(text_col, 1)

        # Quiet action first, so the main one sits at the right edge.
        for action in (item.secondary_action, item.primary_action):
            if action is None:
                continue
            text, callback = action
            button = QPushButton(text)
            if action is item.secondary_action:
                button.setProperty("buttonKind", "quiet")
            elif item.emphasized:
                button.setProperty("buttonKind", "primary")
            button.clicked.connect(callback)
            layout.addWidget(button)


def _replace_rows(layout: QVBoxLayout, rows: list[_ChecklistRow]) -> None:
    while layout.count():
        child = layout.takeAt(0)
        if child.widget():
            child.widget().deleteLater()
    for row in rows:
        layout.addWidget(row)


class SetupChecklistCard(QFrame):
    """The full variant - DESIGN.md §4.8's empty-state card, scoped to
    the Sources page: a title, then every non-dismissed item (done ones
    included, so the card reads as a complete overview, not just a
    to-do list). Never taller than its content; the page positions it."""

    def __init__(self, parent: QWidget = None):
        super().__init__(parent)
        self.setObjectName("setupChecklistCard")
        self.setMaximumWidth(CARD_MAX_WIDTH)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)

        self._heading = QLabel("Finish setting up")
        self._heading.setObjectName("setupChecklistHeading")

        self._rows_layout = QVBoxLayout()
        self._rows_layout.setContentsMargins(0, 0, 0, 0)
        self._rows_layout.setSpacing(theme.SPACING_XS)

        layout = QVBoxLayout(self)
        layout.setSpacing(theme.SPACING_MD)
        layout.addWidget(self._heading)
        layout.addLayout(self._rows_layout)

        self._rows: list[_ChecklistRow] = []

    def update_items(self, items: list[ChecklistItem]) -> None:
        self._rows = [_ChecklistRow(item, self) for item in items]
        _replace_rows(self._rows_layout, self._rows)


class SetupChecklistStrip(QFrame):
    """The compact variant - rows only for what's still outstanding
    (to_do/needs_attention/checking). The first carries the whole DESIGN.md
    §6 strip sentence ("Setup. 2 of 3 done. Connect Claude Desktop so
    Claude can use your files.") with that step's actions, rather than a
    count line above a row that repeats the step."""

    def __init__(self, parent: QWidget = None):
        super().__init__(parent)
        self.setObjectName("setupChecklistStrip")

        self._rows_layout = QVBoxLayout()
        self._rows_layout.setContentsMargins(0, 0, 0, 0)
        self._rows_layout.setSpacing(theme.SPACING_XS)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addLayout(self._rows_layout)

        self._rows: list[_ChecklistRow] = []

    def update_items(self, items: list[ChecklistItem], steps_done: int, total_steps: int) -> None:
        # The page header's own action is the primary one while the strip
        # shows, so no strip button competes with it.
        outstanding = [
            dataclasses.replace(item, emphasized=False)
            for item in items if item.state not in ("done", "dismissed")
        ]
        self._rows = []
        for index, item in enumerate(outstanding):
            markup = None
            if index == 0:
                markup = (
                    f"<span style='font-weight:600'>Setup.</span> {steps_done} of {total_steps} done. "
                    f"{html.escape(item.strip_text or item.label)}"
                )
            self._rows.append(_ChecklistRow(item, self, markup=markup))
        _replace_rows(self._rows_layout, self._rows)
