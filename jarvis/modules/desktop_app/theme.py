"""Shared visual theme - the Cobalt light design system (DESIGN.md at
the repo root is the source of truth for every token below; this module
is brought into line with it, not the other way around).

One QSS stylesheet, built by `build_stylesheet()` and applied exactly
once, at `QApplication` level (`app.py`'s `main()`) - not per-widget.
Views should not add their own `setStyleSheet()` calls for anything
already covered below - the one legitimate exception is per-instance
state that genuinely varies at runtime (a status banner that's colored
differently depending on whether it's showing an error, a success, or
plain info), which goes through Qt dynamic properties
(`setProperty("bannerKind", ...)` + the attribute selectors below), not
a hardcoded color in Python.
"""

import sys

from PySide6.QtCore import QByteArray, Qt
from PySide6.QtGui import QIcon, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer

# Color tokens (DESIGN.md §2.2) - named once here and referenced
# everywhere below, instead of each rule hardcoding its own hex value.
BACKGROUND = "#F5F7FA"
SURFACE = "#FFFFFF"
SURFACE_ALT = "#EEF1F6"
BORDER = "#E4E7EC"
# Outline for buttons and inputs - distinct from the decorative BORDER
# above (cards, rows, dividers), since a control needs the higher,
# 3:1-contrast outline DESIGN.md's own contrast table requires.
BORDER_CONTROL = "#7F899C"
TEXT_PRIMARY = "#111827"
TEXT_SECONDARY = "#5D687C"
TEXT_DISABLED = "#98A2B3"
ACCENT = "#3157D5"
ACCENT_HOVER = "#4A6BDE"
ACCENT_PRESSED = "#2748B8"
# Text color for anything sitting on an ACCENT-strength fill (also used
# on the destructive button's hover/pressed ERROR fill - DESIGN.md
# doesn't define a separate token for that case, and the value is
# identical white either way).
ACCENT_TEXT = "#FFFFFF"
ACCENT_LIGHT = "#E7ECFF"
# Orange for graphics only (the collapsed-nav attention dot, markers) -
# never for text (DESIGN.md §2.2).
ACCENT_SECONDARY = "#C07434"
ERROR = "#C23B31"
ERROR_BG = "#FDECEA"
SUCCESS = "#1F7A4D"
SUCCESS_BG = "#E6F4EC"
# A softer tier than ERROR - for a caveat on an otherwise-successful
# action (e.g. feedback was saved, but the GitHub issue side-effect
# failed), which reads as false alarm in ERROR's red - not "something
# broke," just "one secondary part of this didn't happen."
WARNING = "#9A5A1E"
WARNING_BG = "#FFF4E8"
WARNING_BORDER = "#E8C9A6"
SUCCESS_BORDER = "#B6DCC6"
INFO_BORDER = "#C9D4FB"
# Same value as ACCENT_LIGHT (DESIGN.md §2.2's own note) - kept as a
# separate name since "info banner fill" and "selected nav fill" are
# different roles that happen to share a color today.
INFO_BG = "#E7ECFF"

# Typeface: native, per platform (DESIGN.md §2.3) - Windows gets an
# explicit Segoe UI; macOS gets Qt's own application default font
# instead of naming a family, since `-apple-system` does not resolve
# under Qt (confirmed in the design doc - a real, not hypothetical,
# platform gap). FONT_FAMILY_CSS is the QSS fragment build_stylesheet()
# splices in - empty on macOS/other, so the `*` rule simply omits
# `font-family` and Qt's native default takes over.
FONT_FAMILY = "Segoe UI"
FONT_FAMILY_CSS = f"font-family: {FONT_FAMILY}, sans-serif;" if sys.platform == "win32" else ""

# Sizes in px, never points (DESIGN.md §2.3) - every Python call site
# that sets one of these via QFont must use setPixelSize(...), never
# setPointSize(...).
FONT_SIZE_SMALL = 12
FONT_SIZE_BASE = 13
FONT_SIZE_HEADING = 15
FONT_SIZE_PAGE_TITLE = 19

# A 4px grid (DESIGN.md §2.4): 4, 8, 12, 16, 24, 32. Default gap 12,
# default card padding 16, page padding 20-24 (applied per-layout, not
# a token here).
SPACING_XS = 4
SPACING_SM = 8
SPACING_MD = 12
SPACING_LG = 16
SPACING_XL = 24
SPACING_XXL = 32

# 8 for buttons/inputs/cards/banners; fully round for tags/progress bars
# (set per-rule below, not a second constant); 4 only for small inner
# pieces like a checkbox (overridden locally where that applies).
RADIUS = 8

# Sidebar nav items, DESIGN.md §2.6.
NAV_ITEM_HEIGHT = 40

# List rows (Sources, the setup checklist), DESIGN.md §2.6.
LIST_ROW_MIN_HEIGHT = 48
SOURCE_ICON_TILE = 40

_ICON_SIZE = 20

# Per-connector icons (Sources rows, Search result rows - Figma parity
# pass, Phase 9). Same hand-authored inline-SVG filled style as
# nav_rail.py's own icon set (DESIGN.md §2.7), not a bundled icon font.
FOLDER_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24">'
    '<path fill="currentColor" d="M10 4H4a2 2 0 0 0-2 2v12a2 2 0 0 0 2 2h16a2 2 0 0 0 '
    '2-2V8a2 2 0 0 0-2-2h-8l-2-2z"/></svg>'
)
CLOUD_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24">'
    '<path fill="currentColor" d="M19 18H6a4 4 0 0 1-.6-7.95 5.5 5.5 0 0 1 10.78-1.74A4.5 '
    '4.5 0 0 1 19 18z"/></svg>'
)

# Disclosure chevron (collapsible_section.py, Figma parity pass, Phase
# 10) - same hand-authored inline-SVG convention as the icons above.
CHEVRON_RIGHT_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24">'
    '<path fill="currentColor" d="M8.59 16.59 13.17 12 8.59 7.41 10 6l6 6-6 6z"/></svg>'
)
# Setup checklist item states (setup_checklist.py, DESIGN.md §4.7) - an
# icon per state, so the state never rests on colour alone (§2.2).
CHECK_CIRCLE_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24">'
    '<path fill="currentColor" d="M12 2a10 10 0 1 0 0 20 10 10 0 0 0 0-20zm-2 15-5-5 1.41-1.41L10 '
    '14.17l7.59-7.59L19 8l-9 9z"/></svg>'
)
EMPTY_CIRCLE_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24">'
    '<path fill="currentColor" d="M12 2a10 10 0 1 0 0 20 10 10 0 0 0 0-20zm0 18a8 8 0 1 1 0-16 '
    '8 8 0 0 1 0 16z"/></svg>'
)
ALERT_CIRCLE_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24">'
    '<path fill="currentColor" d="M12 2a10 10 0 1 0 0 20 10 10 0 0 0 0-20zm1 15h-2v-2h2v2zm0-4h-2V7h2v6z"/>'
    '</svg>'
)
CLOCK_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24">'
    '<path fill="currentColor" d="M12 2a10 10 0 1 0 0 20 10 10 0 0 0 0-20zm0 18a8 8 0 1 1 0-16 '
    '8 8 0 0 1 0 16zm.5-13H11v6l5.25 3.15.75-1.23-4.5-2.67V7z"/></svg>'
)
CHEVRON_DOWN_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24">'
    '<path fill="currentColor" d="M7.41 8.59 12 13.17l4.59-4.58L18 10l-6 6-6-6z"/></svg>'
)


def icon_from_svg(svg_str: str, color: str, size: int = _ICON_SIZE) -> QIcon:
    """Renders a hand-authored inline SVG string (`currentColor` standing
    in for the fill) into a colored `QIcon` - shared by `nav_rail.py` and
    anywhere else that needs the same per-state icon recoloring (DESIGN.md
    §2.7), now that it has more than one real caller."""
    colored = svg_str.replace("currentColor", color)
    renderer = QSvgRenderer(QByteArray(colored.encode("utf-8")))
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    renderer.render(painter)
    painter.end()
    return QIcon(pixmap)


def build_stylesheet() -> str:
    """The full app-wide QSS string. A plain f-string, not a template
    file - short enough that a second file would just be indirection,
    and every value in it already lives in this module's own constants
    above."""
    return f"""
    * {{
        {FONT_FAMILY_CSS}
        font-size: {FONT_SIZE_BASE}px;
        color: {TEXT_PRIMARY};
    }}

    QMainWindow, QWidget {{
        background-color: {BACKGROUND};
    }}

    /* A label otherwise paints this rule's own opaque background,
       hiding whatever color its parent container (a themed card, a
       QGroupBox) actually uses underneath it - every QLabel below this
       one now inherits that instead, the same fix already applied
       one-off to bannerLabel before this rule
       existed. */
    QLabel {{
        background: transparent;
    }}

    QPushButton {{
        background-color: {SURFACE};
        border: 1px solid {BORDER_CONTROL};
        border-radius: {RADIUS}px;
        padding: {SPACING_SM}px {SPACING_LG}px;
        color: {TEXT_PRIMARY};
    }}
    QPushButton:hover {{
        background-color: {SURFACE_ALT};
    }}
    QPushButton:pressed {{
        background-color: {BORDER};
    }}
    QPushButton:disabled {{
        background-color: {SURFACE_ALT};
        border-color: {BORDER};
        color: {TEXT_DISABLED};
    }}

    /* The one primary call-to-action per screen (a dialog's submit
       button, Settings' API-key Save) - filled with the accent color by
       default, not just on hover like every other button, so it reads as
       "the" action instead of one gray button among several. */
    QPushButton[buttonKind="primary"] {{
        background-color: {ACCENT};
        border: 1px solid {ACCENT};
        color: {ACCENT_TEXT};
        font-weight: 600;
    }}
    QPushButton[buttonKind="primary"]:hover {{
        background-color: {ACCENT_HOVER};
        border-color: {ACCENT_HOVER};
    }}
    QPushButton[buttonKind="primary"]:pressed {{
        background-color: {ACCENT_PRESSED};
        border-color: {ACCENT_PRESSED};
    }}
    QPushButton[buttonKind="primary"]:disabled {{
        background-color: {SURFACE_ALT};
        border-color: {BORDER};
        color: {TEXT_DISABLED};
    }}

    /* No fill, no outline (DESIGN.md §4.3) - Cancel, Later, "I don't
       use it" - the quietest of the three button kinds. */
    QPushButton[buttonKind="quiet"] {{
        background-color: transparent;
        border: 1px solid transparent;
        color: {TEXT_SECONDARY};
    }}
    QPushButton[buttonKind="quiet"]:hover {{
        background-color: {SURFACE_ALT};
    }}
    QPushButton[buttonKind="quiet"]:pressed {{
        background-color: {BORDER};
    }}
    QPushButton[buttonKind="quiet"]:disabled {{
        color: {TEXT_DISABLED};
    }}

    QLineEdit, QTextEdit {{
        background-color: {SURFACE};
        border: 1px solid {BORDER_CONTROL};
        border-radius: {RADIUS}px;
        padding: {SPACING_SM}px {SPACING_MD}px;
        selection-background-color: {ACCENT};
    }}
    QLineEdit:focus, QTextEdit:focus {{
        outline: 2px solid {ACCENT};
        outline-offset: 2px;
    }}
    QLineEdit:disabled, QTextEdit:disabled {{
        color: {TEXT_DISABLED};
    }}

    QGroupBox {{
        background-color: {SURFACE};
        border: 1px solid {BORDER};
        border-radius: {RADIUS}px;
        margin-top: {SPACING_LG + 6}px;
        padding: {SPACING_LG}px {SPACING_MD}px {SPACING_MD}px {SPACING_MD}px;
    }}
    QGroupBox::title {{
        subcontrol-origin: margin;
        subcontrol-position: top left;
        left: {SPACING_MD}px;
        padding: 0 {SPACING_SM}px;
        color: {TEXT_SECONDARY};
        font-size: {FONT_SIZE_HEADING}px;
        font-weight: 600;
    }}

    QTabWidget::pane {{
        border: 1px solid {BORDER};
        border-radius: {RADIUS}px;
        top: -1px;
    }}
    QTabBar::tab {{
        background: transparent;
        color: {TEXT_SECONDARY};
        padding: {SPACING_MD}px {SPACING_LG}px;
        border-bottom: 2px solid transparent;
    }}
    QTabBar::tab:hover {{
        color: {TEXT_PRIMARY};
    }}
    QTabBar::tab:selected {{
        color: {TEXT_PRIMARY};
        border-bottom: 2px solid {ACCENT};
    }}
    QTabWidget:disabled QTabBar::tab {{
        color: {TEXT_DISABLED};
    }}

    QListWidget, QTreeWidget {{
        background-color: {SURFACE};
        border: 1px solid {BORDER};
        border-radius: {RADIUS}px;
        alternate-background-color: {SURFACE_ALT};
    }}
    QListWidget::item, QTreeWidget::item {{
        padding: {SPACING_SM}px;
    }}
    QListWidget::item:selected, QTreeWidget::item:selected {{
        background-color: {ACCENT};
        color: {ACCENT_TEXT};
    }}
    QListWidget::item:hover, QTreeWidget::item:hover {{
        background-color: {SURFACE_ALT};
    }}

    QRadioButton {{
        spacing: {SPACING_SM}px;
    }}
    QRadioButton::indicator {{
        width: 14px;
        height: 14px;
        border-radius: 7px;
        border: 1px solid {BORDER_CONTROL};
        background-color: {SURFACE};
    }}
    QRadioButton::indicator:checked {{
        border: 1px solid {ACCENT};
        background-color: {ACCENT};
    }}
    QRadioButton::indicator:hover {{
        border-color: {ACCENT_HOVER};
    }}
    QRadioButton:disabled {{
        color: {TEXT_DISABLED};
    }}

    /* SettingsView's scroll area - the app's first, so (like
       QRadioButton above) there's no existing convention to match; this
       is a plain minimal-track scrollbar in the same palette as
       everything else, not an OS-native one standing out against it. */
    QScrollBar:vertical {{
        background: transparent;
        width: 12px;
        margin: 0;
    }}
    QScrollBar::handle:vertical {{
        background-color: {BORDER};
        border-radius: {RADIUS}px;
        min-height: 24px;
    }}
    QScrollBar::handle:vertical:hover {{
        background-color: {ACCENT};
    }}
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
        height: 0;
    }}
    QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{
        background: transparent;
    }}

    QSplitter::handle {{
        background-color: {BORDER};
    }}
    QSplitter::handle:hover {{
        background-color: {ACCENT};
    }}

    QProgressBar {{
        background-color: {SURFACE_ALT};
        border: 1px solid {BORDER};
        border-radius: {RADIUS}px;
        text-align: center;
        color: {TEXT_SECONDARY};
        min-height: 18px;
    }}
    QProgressBar::chunk {{
        background-color: {ACCENT};
        border-radius: {RADIUS}px;
    }}

    QMenuBar {{
        background-color: {BACKGROUND};
        color: {TEXT_PRIMARY};
    }}
    QMenuBar::item:selected {{
        background-color: {SURFACE_ALT};
    }}
    QMenu {{
        background-color: {SURFACE};
        border: 1px solid {BORDER};
    }}
    QMenu::item:selected {{
        background-color: {ACCENT};
        color: {ACCENT_TEXT};
    }}

    /* Status banners (error_presentation.py's DaemonStatusBanner) -
       kind ("error"/"info"/"success") set via a dynamic property, not
       a hardcoded color per instance. */
    DaemonStatusBanner[bannerKind="error"] {{
        background-color: {ERROR_BG};
        border: 1px solid {ERROR};
        border-radius: {RADIUS}px;
    }}
    DaemonStatusBanner[bannerKind="info"] {{
        background-color: {INFO_BG};
        border: 1px solid {INFO_BORDER};
        border-radius: {RADIUS}px;
    }}
    DaemonStatusBanner[bannerKind="success"] {{
        background-color: {SUCCESS_BG};
        border: 1px solid {SUCCESS};
        border-radius: {RADIUS}px;
    }}
    QLabel#bannerLabel[bannerKind="error"] {{
        color: {ERROR};
    }}
    QLabel#bannerLabel[bannerKind="info"] {{
        color: {TEXT_PRIMARY};
    }}
    QLabel#bannerLabel[bannerKind="success"] {{
        color: {SUCCESS};
    }}

    /* Dialog headings and bodies (error_presentation.ConfirmDialog,
       settings_dialog.SettingsDialog). */
    QLabel#dialogTitle {{
        font-size: {FONT_SIZE_HEADING}px;
        font-weight: 600;
    }}
    QLabel#confirmDialogMessage {{
        color: {TEXT_SECONDARY};
    }}

    /* update_banner.py's UpdateBanner - always the info kind (blue),
       DESIGN.md §4.2 - an available update isn't a warning. */
    QWidget#updateBanner {{
        background-color: {INFO_BG};
        border: 1px solid {INFO_BORDER};
        border-radius: {RADIUS}px;
    }}

    /* The sent/partly-sent card in feedback_dialog.py. */
    QFrame#feedbackResultCard {{
        background-color: {SURFACE_ALT};
        border: 1px solid {BORDER};
        border-radius: {RADIUS}px;
    }}

    /* Syncing row's inline progress (connectors_view.py's SyncedRootRow,
       Stitch "Sources - Syncing"). */
    QFrame#syncProgressPanel {{
        background-color: {BACKGROUND};
        border: 1px solid {BORDER};
        border-radius: {RADIUS}px;
        padding: {SPACING_MD}px;
    }}
    QLabel#syncProgressText {{
        color: {TEXT_SECONDARY};
        font-size: {FONT_SIZE_SMALL}px;
    }}
    QProgressBar#syncProgressBar {{
        min-height: 6px;
        max-height: 6px;
        background-color: {BORDER};
        border: none;
        border-radius: 3px;
    }}
    QProgressBar#syncProgressBar::chunk {{
        background-color: {ACCENT};
        border-radius: 3px;
    }}

    /* Setup checklist (setup_checklist.py, DESIGN.md §4.7) - itemState
       set per-row-label the same dynamic-property way as the banners
       above. */
    QFrame#setupChecklistCard {{
        background-color: {SURFACE};
        border: 1px solid {BORDER};
        border-radius: {RADIUS}px;
        padding: {SPACING_LG}px;
    }}
    QFrame#setupChecklistStrip {{
        background-color: {SURFACE_ALT};
        border: 1px solid {BORDER};
        border-radius: {RADIUS}px;
        padding: {SPACING_MD}px;
    }}
    QLabel#setupChecklistHeading {{
        color: {TEXT_PRIMARY};
        font-size: {FONT_SIZE_HEADING}px;
        font-weight: 600;
    }}
    QFrame#setupChecklistRow {{
        background: transparent;
        min-height: {LIST_ROW_MIN_HEIGHT}px;
    }}
    QLabel#setupChecklistRowLabel[itemState="done"] {{
        color: {TEXT_SECONDARY};
    }}
    QLabel#setupChecklistRowLabel[itemState="to_do"] {{
        color: {TEXT_PRIMARY};
    }}
    QLabel#setupChecklistRowLabel[itemState="needs_attention"] {{
        color: {WARNING};
        font-weight: 600;
    }}
    QLabel#setupChecklistRowLabel[itemState="checking"] {{
        color: {TEXT_DISABLED};
        font-style: italic;
    }}
    QLabel#setupChecklistRowMessage {{
        color: {TEXT_SECONDARY};
        font-size: {FONT_SIZE_SMALL}px;
    }}

    /* Per-page header bar (page_header.py, Figma parity pass) - a
       divider under the title/action row, not a card: border on the
       bottom edge only. */
    QFrame#pageHeader {{
        background-color: {SURFACE};
        border-bottom: 1px solid {BORDER};
        padding: {SPACING_LG}px {SPACING_XL}px;
    }}
    QLabel#pageHeaderTitle {{
        color: {TEXT_PRIMARY};
        font-size: {FONT_SIZE_PAGE_TITLE}px;
        font-weight: 600;
    }}

    /* Search's pre-first-search placeholder (views/search_view.py) -
       same named-QLabel convention as the sync status line above. The
       zero-results case now uses EmptyStateCard (below) instead. */
    /* Stands in for the Topics passages list until a topic is open, so
       it wears the same surface and border as that list. */
    QLabel#topicsPassagesHint {{
        background-color: {SURFACE};
        border: 1px solid {BORDER};
        border-radius: {RADIUS}px;
        color: {TEXT_SECONDARY};
        padding: {SPACING_LG}px;
    }}
    QLabel#searchEmptyState {{
        color: {TEXT_SECONDARY};
        padding: {SPACING_LG * 2}px;
    }}

    /* Generic empty-state card (empty_state.py, DESIGN.md §4.8) - Search's
       "no results" and Topics' "no topics yet". Not used by the Sources
       page's own empty state (setup_checklist.SetupChecklistCard stays
       its own, richer thing). */
    QFrame#emptyStateCard {{
        background-color: {SURFACE};
        border: 1px solid {BORDER};
        border-radius: {RADIUS}px;
        padding: {SPACING_XL}px;
    }}
    QLabel#emptyStateTitle {{
        color: {TEXT_PRIMARY};
        font-size: {FONT_SIZE_HEADING}px;
        font-weight: 600;
    }}
    QLabel#emptyStateMessage {{
        color: {TEXT_SECONDARY};
    }}

    /* Generic inline status message (sign_in_dialog.py, feedback_dialog.py)
       - messageKind set via setProperty, same dynamic-property convention
       as the banners/stepper above, not a hardcoded color per call site. */
    QLabel[messageKind="error"] {{
        color: {ERROR};
    }}
    QLabel[messageKind="success"] {{
        color: {SUCCESS};
    }}
    QLabel[messageKind="info"] {{
        color: {TEXT_SECONDARY};
    }}
    QLabel[messageKind="warning"] {{
        color: {WARNING};
    }}

    /* Nav rail (views/nav_rail.py) - checked/hover state set via the
       same dynamic-property convention as the banners above rather than
       a bespoke selector. Icon recoloring (default/selected) happens in
       Python (nav_rail.py swaps a pre-rendered QIcon) since QSS alone
       can't recolor an already-rendered pixmap. */
    QToolButton[navKind="rail"] {{
        background: transparent;
        border: 1px solid transparent;
        border-radius: {RADIUS}px;
        padding: 0px {SPACING_MD}px;
        min-height: {NAV_ITEM_HEIGHT}px;
        color: {TEXT_SECONDARY};
        text-align: left;
    }}
    QToolButton[navKind="rail"]:hover {{
        background-color: {SURFACE_ALT};
    }}
    QToolButton[navKind="rail"]:checked {{
        background-color: {ACCENT_LIGHT};
        border-color: {ACCENT_LIGHT};
        color: {ACCENT};
        font-weight: 600;
    }}
    QToolButton[navKind="rail"]:focus {{
        outline: none;
        border: 2px solid {ACCENT};
        padding: 0px {SPACING_MD - 1}px;
    }}

    /* Sources nav-rail tag (nav_rail.py's _SourcesTag, DESIGN.md §4.1) -
       a quiet pill when expanded, a small dot when collapsed (the
       [collapsed="true"] rules below override size/shape/border, not a
       second widget). */
    QLabel#sourcesTag[tagKind="syncing"] {{
        background-color: {INFO_BG};
        border: 1px solid {INFO_BORDER};
        color: {ACCENT};
        border-radius: 7px;
        font-size: 11px;
        font-weight: 600;
        padding: 1px {SPACING_SM}px;
    }}
    QLabel#sourcesTag[tagKind="attention"] {{
        background-color: {WARNING_BG};
        border: 1px solid {WARNING_BORDER};
        color: {WARNING};
        border-radius: 7px;
        font-size: 11px;
        font-weight: 600;
        padding: 1px {SPACING_SM}px;
    }}
    QLabel#sourcesTag[collapsed="true"][tagKind="syncing"] {{
        background-color: {ACCENT};
        border: none;
        padding: 0px;
        min-width: 10px;
        max-width: 10px;
        min-height: 10px;
        max-height: 10px;
        border-radius: 5px;
    }}
    QLabel#sourcesTag[collapsed="true"][tagKind="attention"] {{
        background-color: {ACCENT_SECONDARY};
        border: none;
        padding: 0px;
        min-width: 10px;
        max-width: 10px;
        min-height: 10px;
        max-height: 10px;
        border-radius: 5px;
    }}

    /* Sources row (connectors_view.py's SyncedRootRow, DESIGN.md §4.6) -
       a separate, distinctly-named component from the nav rail's own
       sourcesTag above (not shared - this codebase's established
       per-module duplication), same visual convention (colors/radius/
       weight). */
    QFrame#sourceRow {{
        background-color: {SURFACE};
        border: 1px solid {BORDER};
        border-radius: {RADIUS}px;
        min-height: {LIST_ROW_MIN_HEIGHT}px;
        padding: {SPACING_MD}px {SPACING_LG}px;
    }}
    QFrame#sourceIconTile {{
        background-color: {SURFACE_ALT};
        border: 1px solid {BORDER};
        border-radius: {RADIUS}px;
        padding: 0px;
    }}
    QScrollArea#sourcesScroll, QWidget#sourcesList {{
        background: transparent;
        border: none;
    }}
    QLabel#sourcesListTitle {{
        color: {TEXT_PRIMARY};
        font-size: {FONT_SIZE_HEADING}px;
        font-weight: 600;
    }}
    QLabel#sourcesCountPill {{
        background-color: {SURFACE_ALT};
        border: 1px solid {BORDER};
        border-radius: 9px;
        color: {TEXT_SECONDARY};
        font-size: {FONT_SIZE_SMALL}px;
        padding: 1px {SPACING_SM}px;
    }}
    /* add_source_dialog.py's option tiles - a whole-card button. */
    QPushButton#sourceOptionTile {{
        background-color: {SURFACE};
        border: 1px solid {BORDER};
        border-radius: {RADIUS}px;
        padding: 0px;
        text-align: left;
    }}
    QPushButton#sourceOptionTile:hover {{
        background-color: {SURFACE_ALT};
        border-color: {BORDER_CONTROL};
    }}
    QPushButton#sourceOptionTile:pressed {{
        background-color: {BORDER};
    }}
    QPushButton#sourceOptionTile:focus {{
        outline: none;
        border: 2px solid {ACCENT};
        padding: 0px;
    }}
    QLabel#sourceOptionTitle {{
        font-weight: 600;
    }}
    QLabel#sourceOptionDescription {{
        color: {TEXT_SECONDARY};
        font-size: {FONT_SIZE_SMALL}px;
    }}
    QLabel#dialogHelperText {{
        color: {TEXT_SECONDARY};
    }}
    QLabel#navBrandMark {{
        background-color: {ACCENT};
        color: {ACCENT_TEXT};
        border-radius: {RADIUS}px;
        font-weight: 600;
    }}
    QLabel#navBrandName {{
        color: {TEXT_PRIMARY};
        font-size: {FONT_SIZE_HEADING}px;
        font-weight: 600;
    }}
    QLabel#navBrandSub {{
        color: {TEXT_SECONDARY};
        font-size: {FONT_SIZE_SMALL}px;
    }}
    QLabel#sourceRowName {{
        font-weight: 600;
    }}
    QLabel#sourceRowSecondary {{
        color: {TEXT_SECONDARY};
        font-size: {FONT_SIZE_SMALL}px;
    }}
    QLabel#sourceRowTag[tagKind="synced"] {{
        background-color: {SUCCESS_BG};
        border: 1px solid {SUCCESS_BORDER};
        color: {SUCCESS};
        border-radius: 7px;
        font-size: 11px;
        font-weight: 600;
        padding: 1px {SPACING_SM}px;
    }}
    QLabel#sourceRowTag[tagKind="syncing"] {{
        background-color: {INFO_BG};
        border: 1px solid {INFO_BORDER};
        color: {ACCENT};
        border-radius: 7px;
        font-size: 11px;
        font-weight: 600;
        padding: 1px {SPACING_SM}px;
    }}

    /* Collapsible section header (collapsible_section.py, Figma parity
       pass, Phase 10) - a plain clickable row, not a card: no border,
       just a hover fill so it reads as interactive. */
    QFrame#collapsibleSectionHeader {{
        background-color: transparent;
        border-radius: {RADIUS}px;
    }}
    QFrame#collapsibleSectionHeader:hover {{
        background-color: {SURFACE_ALT};
    }}
    QLabel#collapsibleSectionTitle {{
        color: {TEXT_PRIMARY};
        font-weight: 600;
    }}

    /* Settings page status pill (Claude connection, Figma parity pass,
       Phase 10) - same fully-round-tag convention as sourceRowTag,
       duplicated rather than shared (this codebase's established
       per-module seam). */
    QLabel#settingsStatusPill[tagKind="connected"] {{
        background-color: {SUCCESS_BG};
        border: 1px solid {SUCCESS_BORDER};
        color: {SUCCESS};
        border-radius: 7px;
        font-size: 11px;
        font-weight: 600;
        padding: 1px {SPACING_SM}px;
    }}
    QLabel#settingsStatusPill[tagKind="needs_repair"] {{
        background-color: {WARNING_BG};
        border: 1px solid {WARNING_BORDER};
        color: {WARNING};
        border-radius: 7px;
        font-size: 11px;
        font-weight: 600;
        padding: 1px {SPACING_SM}px;
    }}
    QLabel#settingsStatusPill[tagKind="not_connected"] {{
        background-color: {SURFACE_ALT};
        border: 1px solid {BORDER};
        color: {TEXT_SECONDARY};
        border-radius: 7px;
        font-size: 11px;
        font-weight: 600;
        padding: 1px {SPACING_SM}px;
    }}

    /* Destructive actions (e.g. the Settings page's Force Kill button) -
       ERROR token instead of the default QPushButton palette above. */
    QPushButton[buttonKind="destructive"] {{
        background-color: {ERROR_BG};
        border: 1px solid {ERROR};
        color: {ERROR};
    }}
    QPushButton[buttonKind="destructive"]:hover {{
        background-color: {ERROR};
        border-color: {ERROR};
        color: {ACCENT_TEXT};
    }}
    QPushButton[buttonKind="destructive"]:pressed {{
        background-color: {ERROR};
        border-color: {ERROR};
    }}

    /* Keyboard focus (DESIGN.md §2.5). Qt ignores an outline's width and
       offset on buttons and draws the platform focus box tight around the
       label instead, clipping it ("Cance"). So the ring is the border:
       2px, with the padding 1px smaller so the label doesn't move. Last in
       the sheet so it wins over each button kind's own border. */
    QPushButton:focus,
    QPushButton[buttonKind="quiet"]:focus,
    QPushButton[buttonKind="destructive"]:focus {{
        outline: none;
        border: 2px solid {ACCENT};
        padding: {SPACING_SM - 1}px {SPACING_LG - 1}px;
    }}
    /* An ACCENT ring would vanish against the primary button's ACCENT fill. */
    QPushButton[buttonKind="primary"]:focus {{
        outline: none;
        border: 2px solid {TEXT_PRIMARY};
        padding: {SPACING_SM - 1}px {SPACING_LG - 1}px;
    }}
    """
