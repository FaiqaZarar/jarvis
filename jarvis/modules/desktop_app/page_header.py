"""Per-page header bar (Figma parity pass, Phase 9) - a bold page title
plus the page's one primary action, in a bordered strip separating it
from the page content below. Every page in the real Figma reference
(`docs/design/...` - see the Phase 9 plan) has this; the current build
had each view place its own buttons directly in the page body with no
such frame, which reads as noticeably less organized side by side with
the reference.

One action only, not a toolbar (DESIGN.md principle 2's "exactly one
primary action", already the convention `error_presentation.
DaemonStatusBanner`'s own action slot follows) - a page that needs no
action (Search, this phase) just omits it.
"""

from typing import Callable, Optional

from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton, QWidget


class PageHeader(QFrame):
    """`QFrame`, not `QWidget` - a plain `QWidget` subclass never paints
    its own QSS background/border without `WA_StyledBackground` set
    (DESIGN.md §8's confirmed banner gap); every other bordered
    container in this codebase (`setup_checklist`'s cards, `empty_state.
    EmptyStateCard`) sidesteps that by being a `QFrame` instead, which
    paints natively."""

    def __init__(
        self, title: str, action: Optional[tuple[str, Callable[[], None]]] = None,
        parent: QWidget = None, *, primary: bool = True,
    ):
        """`primary=False` for a routine action (Topics' Refresh) on a
        page whose state already has its own primary one."""
        super().__init__(parent)
        self.setObjectName("pageHeader")

        self._title_label = QLabel(title)
        self._title_label.setObjectName("pageHeaderTitle")

        layout = QHBoxLayout(self)
        layout.addWidget(self._title_label)
        layout.addStretch(1)

        # Public, not `_action_button` - callers that already own a named
        # reference to this same button (ConnectorsView's `_add_source_
        # button`, wired into `_set_controls_enabled` and asserted on
        # directly in tests) point it at this instance rather than
        # building a second button PageHeader would just duplicate.
        self.action_button: Optional[QPushButton] = None
        if action is not None:
            label, callback = action
            self.action_button = QPushButton(label)
            if primary:
                self.action_button.setProperty("buttonKind", "primary")
            self.action_button.clicked.connect(callback)
            layout.addWidget(self.action_button)
