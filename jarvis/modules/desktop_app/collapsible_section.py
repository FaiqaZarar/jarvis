"""Collapsible section (Figma parity pass, Phase 10) - a clickable
header (chevron + title) that shows/hides a body of arbitrary widgets.
Built for Settings' new "Advanced" section (daemon diagnostics the real
reference doesn't show in its primary view - DESIGN.md principle 7,
"complexity stays beneath the surface"), but generic: any future
collapsed-by-default group of controls can reuse it rather than
hand-rolling another disclosure triangle.
"""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QVBoxLayout, QWidget

from jarvis.modules.desktop_app import theme


class CollapsibleSection(QFrame):
    def __init__(self, title: str, *, expanded: bool = False, parent: QWidget = None):
        super().__init__(parent)
        self._expanded = expanded

        self._header = QFrame()
        self._header.setObjectName("collapsibleSectionHeader")
        self._header.setCursor(Qt.CursorShape.PointingHandCursor)
        self._header.mousePressEvent = lambda _event: self.toggle()

        self._chevron_label = QLabel()
        self._title_label = QLabel(title)
        self._title_label.setObjectName("collapsibleSectionTitle")

        header_layout = QHBoxLayout(self._header)
        header_layout.addWidget(self._chevron_label)
        header_layout.addWidget(self._title_label)
        header_layout.addStretch(1)

        self._body = QWidget()
        self._body_layout = QVBoxLayout(self._body)
        self._body_layout.setContentsMargins(theme.SPACING_LG, 0, 0, 0)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._header)
        layout.addWidget(self._body)

        self._apply_expanded_state()

    def add_widget(self, widget: QWidget) -> None:
        self._body_layout.addWidget(widget)

    def toggle(self) -> None:
        self._expanded = not self._expanded
        self._apply_expanded_state()

    def _apply_expanded_state(self) -> None:
        self._body.setVisible(self._expanded)
        icon = theme.icon_from_svg(
            theme.CHEVRON_DOWN_SVG if self._expanded else theme.CHEVRON_RIGHT_SVG,
            theme.TEXT_SECONDARY, 16,
        )
        self._chevron_label.setPixmap(icon.pixmap(16, 16))
