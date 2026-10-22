"""The generic empty-state card (DESIGN.md §4.8): a title, an optional
one-sentence message, an optional single action - used wherever a view
has nothing to show yet and should point to what to do next.

Not reused for the Sources page's own empty state - `setup_checklist.
SetupChecklistCard` is deliberately richer there (checklist rows, not
just one sentence) and stays Sources-specific. This is the plain
version, for Search's "no results" and Topics' "no topics yet"."""

from typing import Callable, Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton, QSizePolicy, QVBoxLayout, QWidget

from jarvis.modules.desktop_app import theme

MAX_WIDTH = 480


def centred_row(widget: QWidget) -> QHBoxLayout:
    """`widget` centred, growing up to its maximum width. Not an alignment
    flag: an aligned widget never grows past its size hint."""
    row = QHBoxLayout()
    row.setContentsMargins(0, 0, 0, 0)
    row.addStretch(1)
    row.addWidget(widget, 100)
    row.addStretch(1)
    return row


class EmptyStateCard(QWidget):
    """The card sits centred near the top of whatever space the page
    gives this widget, only as tall as its content - the rest stays empty
    rather than stretching the card or anything else on the page."""

    def __init__(
        self, title: str, message: str = "",
        action_text: Optional[str] = None, on_action: Optional[Callable[[], None]] = None,
        parent: QWidget = None,
    ):
        super().__init__(parent)
        self._frame = QFrame()
        self._frame.setObjectName("emptyStateCard")
        self._frame.setMaximumWidth(MAX_WIDTH)
        self._frame.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)

        title_label = QLabel(title)
        title_label.setObjectName("emptyStateTitle")
        title_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title_label.setWordWrap(True)

        layout = QVBoxLayout(self._frame)
        layout.setSpacing(theme.SPACING_SM)
        layout.addWidget(title_label)

        if message:
            message_label = QLabel(message)
            message_label.setObjectName("emptyStateMessage")
            message_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            message_label.setWordWrap(True)
            layout.addWidget(message_label)

        if action_text and on_action:
            button = QPushButton(action_text)
            button.setProperty("buttonKind", "primary")
            button.clicked.connect(on_action)
            layout.addSpacing(theme.SPACING_SM)
            button_row = QHBoxLayout()
            button_row.addStretch(1)
            button_row.addWidget(button)
            button_row.addStretch(1)
            layout.addLayout(button_row)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addLayout(centred_row(self._frame))
        outer.addStretch(1)
