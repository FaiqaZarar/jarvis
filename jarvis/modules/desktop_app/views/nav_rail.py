"""Expandable/collapsible sidebar navigation (DESIGN.md §4.1) replacing
the old top `QTabWidget` for the app's five top-level pages.

Icons are hand-authored inline SVGs, not emoji glyphs or a bundled icon
font - PySide6 already ships `QtSvg`, so no new pip dependency. Each nav
item renders two `QIcon`s at construction - `TEXT_SECONDARY` (default)
and `ACCENT` (selected) - swapped via the button's own `toggled` signal,
since Qt has no way to recolor an already-rendered pixmap through QSS
alone. Hover does not get a third icon color: DESIGN.md §4.1's own
item-states line only specifies a background fill for hover, nothing
calls for a distinct hover icon color.

Collapse/expand (§4.1, §2.6): a non-checkable Collapse control at the
foot, below Settings, toggles every button between icon+label and
icon-only (`QToolButtonStyle`, no rebuild needed) and resizes the whole
rail between 208px (expanded) and 62px (collapsed). The choice persists
across restarts via `QSettings`, under its own `_settings()` seam -
mirrors `main_window.py`'s identical seam for window geometry, not
imported from it, since this codebase's convention is a small seam per
module rather than a shared settings module.

The Sources tag (Syncing/Attention, §4.1): a quiet blue "Syncing" pill or
amber "Attention" pill next to the Sources item when expanded, a small
colored dot when collapsed. It is a child of the Sources button, laid
over it rather than beside it: as a layout sibling it squeezed the
button's icon out of the 62px collapsed rail and was cut off at the
expanded rail's edge. `_SourcesTag` is the one place that isn't
pure QSS - Qt can restyle a pill into a dot via the `collapsed` dynamic
property, but can't blank the pill's text itself, so that one piece of
logic lives in Python. `set_syncing`/`set_needs_attention` are two
independent inputs (an in-flight sync vs. a setup-health problem);
`NavRail` resolves them to one combined state itself - syncing wins when
both are true, since it's the literally-true thing happening right now.
"""

from typing import Optional

from PySide6.QtCore import QEvent, QObject, QSettings, QSize, Qt, Signal
from PySide6.QtWidgets import QButtonGroup, QHBoxLayout, QLabel, QSizePolicy, QToolButton, QVBoxLayout, QWidget

from jarvis.modules.desktop_app import theme
from jarvis.modules.desktop_app.theme import icon_from_svg

_ICON_SIZE = 20

_EXPANDED_WIDTH = 208
_COLLAPSED_WIDTH = 62
_SETTINGS_KEY = "NavRail/collapsed"

_SEARCH_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24">'
    '<path fill="currentColor" d="M15.5 14h-.79l-.28-.27a6.5 6.5 0 1 0-.7.7l.27.28v.79l5 '
    '4.99L20.49 19l-4.99-5zm-6 0a4.5 4.5 0 1 1 0-9 4.5 4.5 0 0 1 0 9z"/></svg>'
)
_HIERARCHY_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24">'
    '<path fill="currentColor" d="M6 3a2 2 0 1 1 0 4 2 2 0 0 1 0-4zm0 6a2 2 0 1 1 0 4 2 2 0 0 1 '
    '0-4zm0 6a2 2 0 1 1 0 4 2 2 0 0 1 0-4zm3-9h9v2H9V6zm0 6h6v2H9v-2zm0 6h9v2H9v-2z"/></svg>'
)
_CONNECTORS_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24">'
    '<path fill="currentColor" d="M3.9 12a5 5 0 0 1 5-5h2v2h-2a3 3 0 0 0 0 6h2v2h-2a5 5 0 0 '
    '1-5-5zm7-1h6v2h-6v-2zm4-4h2a5 5 0 0 1 0 10h-2v-2h2a3 3 0 0 0 0-6h-2V7z"/></svg>'
)
_SETTINGS_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24">'
    '<path fill="currentColor" d="M19.14 12.94a7.14 7.14 0 0 0 0-1.88l2.03-1.58a.5.5 0 0 0 '
    '.12-.64l-1.92-3.32a.5.5 0 0 0-.6-.22l-2.39.96a7.3 7.3 0 0 0-1.62-.94l-.36-2.54a.5.5 0 0 '
    '0-.5-.42h-3.84a.5.5 0 0 0-.5.42l-.36 2.54c-.59.24-1.13.56-1.62.94l-2.39-.96a.5.5 0 0 '
    '0-.6.22L2.71 8.84a.5.5 0 0 0 .12.64l2.03 1.58a7.14 7.14 0 0 0 0 1.88l-2.03 1.58a.5.5 0 '
    '0 0-.12.64l1.92 3.32c.14.24.42.32.6.22l2.39-.96c.49.38 1.03.7 1.62.94l.36 2.54a.5.5 0 0 '
    '0 .5.42h3.84a.5.5 0 0 0 .5-.42l.36-2.54c.59-.24 1.13-.56 1.62-.94l2.39.96c.24.1.46.02.6-.22l1.92-3.32a.5.5 '
    '0 0 0-.12-.64l-2.03-1.58zM12 15.5a3.5 3.5 0 1 1 0-7 3.5 3.5 0 0 1 0 7z"/></svg>'
)
_STATS_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24">'
    '<path fill="currentColor" d="M4 20V10h4v10H4zm6 0V4h4v16h-4zm6 0v-7h4v7h-4z"/></svg>'
)
_COLLAPSE_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24">'
    '<path fill="currentColor" d="M18.41 7.41 17 6l-6 6 6 6 1.41-1.41L13.83 12zM11 6l-6 '
    '6 6 6 1.41-1.41L7.83 12 12.41 7.41z"/></svg>'
)
# The same chevrons pointing the other way, for the Expand action.
_EXPAND_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24">'
    '<path fill="currentColor" d="M5.59 7.41 7 6l6 6-6 6-1.41-1.41L10.17 12zm6 0L13 6l6 6-6 '
    '6-1.41-1.41L16.17 12z"/></svg>'
)

# Order here is the QStackedWidget page index MainWindow builds, and
# must stay in lockstep with its page-construction order - DESIGN.md
# §4.1's nav order: Sources, Search, Topics, Activity, Settings.
NAV_ITEMS: list[tuple[str, str]] = [
    ("Sources", _CONNECTORS_SVG),
    ("Search", _SEARCH_SVG),
    ("Topics", _HIERARCHY_SVG),
    ("Activity", _STATS_SVG),
    ("Settings", _SETTINGS_SVG),
]


def _settings() -> QSettings:
    # A seam, not an inline QSettings() call: tests monkeypatch this to
    # point at an isolated .ini file instead of the real Windows registry
    # (or the real user's config file on Mac/Linux). Mirrors
    # main_window.py's own _settings() seam exactly, duplicated rather
    # than imported - this codebase's convention is a small seam per
    # module, not a shared settings module.
    return QSettings()


class _SourcesTag(QLabel):
    """The Sources nav item's status tag (DESIGN.md §4.1), overlaid on
    `button`: centred at its right edge when expanded, a dot at its
    top-right corner when collapsed. Hidden whenever no kind applies -
    not styled-but-invisible, actually hidden, same convention as the
    setup checklist's dismissed rows."""

    def __init__(self, button: QToolButton):
        super().__init__(button)
        self.setObjectName("sourcesTag")
        self._collapsed = False
        button.installEventFilter(self)
        self.hide()

    def set_state(self, kind: Optional[str], collapsed: bool) -> None:
        self._collapsed = collapsed
        if kind is None:
            self.hide()
            return
        self.setText("" if collapsed else ("Syncing" if kind == "syncing" else "Attention"))
        self.setProperty("tagKind", kind)
        self.setProperty("collapsed", collapsed)
        self.style().unpolish(self)
        self.style().polish(self)
        self.show()
        self._reposition()

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if event.type() == QEvent.Type.Resize:
            self._reposition()
        return False

    def _reposition(self) -> None:
        self.adjustSize()
        button = self.parentWidget()
        if self._collapsed:
            self.move(button.width() - self.width() - theme.SPACING_SM // 2, theme.SPACING_SM // 2)
        else:
            self.move(button.width() - self.width() - theme.SPACING_SM, (button.height() - self.height()) // 2)


class NavRail(QWidget):
    """Five checkable, mutually-exclusive nav buttons plus a non-checkable
    Collapse control at the foot (never added to `self._group` - it
    isn't a page). Emits the clicked nav button's index; `MainWindow`
    wires that straight to its `QStackedWidget.setCurrentIndex`."""

    page_selected = Signal(int)

    def __init__(self, parent: QWidget = None):
        super().__init__(parent)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(theme.SPACING_SM, theme.SPACING_MD, theme.SPACING_SM, theme.SPACING_MD)
        layout.setSpacing(theme.SPACING_SM)

        self._brand_mark = QLabel("J")
        self._brand_mark.setObjectName("navBrandMark")
        self._brand_mark.setFixedSize(32, 32)
        self._brand_mark.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._brand_text = QWidget()
        brand_text_layout = QVBoxLayout(self._brand_text)
        brand_text_layout.setContentsMargins(0, 0, 0, 0)
        brand_text_layout.setSpacing(0)
        brand_name = QLabel("Jarvis")
        brand_name.setObjectName("navBrandName")
        brand_sub = QLabel("Desktop")
        brand_sub.setObjectName("navBrandSub")
        brand_text_layout.addWidget(brand_name)
        brand_text_layout.addWidget(brand_sub)
        brand_row = QHBoxLayout()
        brand_row.setContentsMargins(theme.SPACING_SM, 0, 0, theme.SPACING_MD)
        brand_row.setSpacing(theme.SPACING_SM)
        brand_row.addWidget(self._brand_mark)
        brand_row.addWidget(self._brand_text, 1)
        layout.addLayout(brand_row)

        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        self._nav_buttons: list[QToolButton] = []
        for index, (label, svg) in enumerate(NAV_ITEMS):
            button = QToolButton()
            button.setCheckable(True)
            button.setText(label)
            button.setToolTip(label)
            button.setAccessibleName(label)
            button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
            # Two pre-rendered icons, swapped on toggle - QSS alone can't
            # recolor an already-rendered pixmap.
            button._icon_default = icon_from_svg(svg, theme.TEXT_SECONDARY)
            button._icon_selected = icon_from_svg(svg, theme.ACCENT)
            button.setIcon(button._icon_default)
            button.setIconSize(QSize(_ICON_SIZE, _ICON_SIZE))
            button.setProperty("navKind", "rail")
            button.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            button.setFixedHeight(theme.NAV_ITEM_HEIGHT)
            button.toggled.connect(
                lambda checked, b=button: b.setIcon(b._icon_selected if checked else b._icon_default)
            )
            self._group.addButton(button, index)
            self._nav_buttons.append(button)
            # Settings (the last item) sits at the foot, above Collapse -
            # a stretch inserted before it, not after all five, pushes
            # it down without pushing Collapse down too. By index, not
            # by matching the string "Settings" - this assumes Settings
            # stays last in NAV_ITEMS.
            if index == len(NAV_ITEMS) - 1:
                layout.addStretch(1)
            layout.addWidget(button)
        # Sources (always index 0 - NAV_ITEMS' first entry) is the only
        # item with a tag.
        self._sources_tag = _SourcesTag(self._nav_buttons[0])

        self._collapse_button = QToolButton()
        self._collapse_button.setCheckable(False)
        self._collapse_button.setProperty("navKind", "rail")
        self._collapse_button.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._collapse_button.setFixedHeight(theme.NAV_ITEM_HEIGHT)
        self._collapse_button.setIconSize(QSize(_ICON_SIZE, _ICON_SIZE))
        self._collapse_button.clicked.connect(self._on_collapse_clicked)
        layout.addWidget(self._collapse_button)

        self._group.idClicked.connect(self.page_selected)
        self._group.button(0).setChecked(True)

        self._syncing = False
        self._needs_attention = False

        self._collapsed = False
        self._apply_collapsed(_settings().value(_SETTINGS_KEY, False, type=bool))

    def select_page(self, index: int) -> None:
        """Syncs the rail's own visual selected state to a page switch
        that originated elsewhere (e.g. a view's own "go to X" action) -
        deliberately not routed through `self._group`'s `idClicked`/
        `page_selected`, since `QAbstractButton.setChecked(True)` doesn't
        emit those (only a real user click does), and emitting
        `page_selected` here would be redundant anyway - the caller
        already knows it's switching to `index` on its own."""
        self._group.button(index).setChecked(True)

    def set_syncing(self, syncing: bool) -> None:
        self._syncing = syncing
        self._update_sources_tag()

    def set_needs_attention(self, needs_attention: bool) -> None:
        self._needs_attention = needs_attention
        self._update_sources_tag()

    def _update_sources_tag(self) -> None:
        # Syncing wins when both apply - it's the literally-true thing
        # happening right now; Attention reappears once the sync ends.
        kind = "syncing" if self._syncing else "attention" if self._needs_attention else None
        self._sources_tag.set_state(kind, self._collapsed)

    def _apply_collapsed(self, collapsed: bool) -> None:
        self._collapsed = collapsed
        style = Qt.ToolButtonStyle.ToolButtonIconOnly if collapsed else Qt.ToolButtonStyle.ToolButtonTextBesideIcon
        for button in (*self._nav_buttons, self._collapse_button):
            button.setToolButtonStyle(style)
        # The label names the action a click performs, not the current
        # state - "Expand" while collapsed, "Collapse" while expanded.
        label = "Expand" if collapsed else "Collapse"
        self._collapse_button.setText(label)
        self._collapse_button.setIcon(
            icon_from_svg(_EXPAND_SVG if collapsed else _COLLAPSE_SVG, theme.TEXT_SECONDARY)
        )
        self._brand_text.setVisible(not collapsed)
        self._collapse_button.setToolTip(label)
        self._collapse_button.setAccessibleName(label)
        self.setFixedWidth(_COLLAPSED_WIDTH if collapsed else _EXPANDED_WIDTH)
        self._update_sources_tag()

    def _on_collapse_clicked(self) -> None:
        collapsed = not self._collapsed
        self._apply_collapsed(collapsed)
        _settings().setValue(_SETTINGS_KEY, collapsed)
