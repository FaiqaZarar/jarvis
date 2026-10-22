"""Search view (design doc §2a; result-row visual redesign per
docs/desktop-app-ux-redesign-DESIGN.md §5/§6 - the "Search result item
delegate" listed there as a Medium-effort item).

Query box, results list bound to `SearchResult.results` (`list[ChunkHit]`),
"load more" via `next_cursor` - matching how `search()` already expects
to be called, not a new pagination scheme. Filters (`SearchFilters` -
topic, source, date range, file type) are an explicit follow-up per the
design doc, not built in this pass.

**`SearchResultDelegate`** replaces `format_hit()`'s single plain-text
block with the redesign's specified layout - a small topic tag, a
bolded document title, and secondary-color preview text - painted
directly via `QStyledItemDelegate`, not a richer text format on the
`QListWidgetItem` itself (Qt's item widgets don't support per-run
styling like a real rich-text renderer would). `format_hit()` itself is
unchanged and still set as each item's own text - it's what a screen
reader or `.text()` caller sees; the delegate only changes how the item
*paints*, reading the original `ChunkHit` back via `Qt.UserRole` instead
of re-parsing that formatted string. `_hit_fields()` is the one place
both `format_hit()` and the delegate derive topic/title/preview from,
so the two can't drift apart.
"""

from typing import Optional

from PySide6.QtCore import QModelIndex, QRect, QSize, Qt, QThreadPool, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QVBoxLayout,
    QWidget,
)

from jarvis.modules.daemon import client as daemon_client
from jarvis.modules.desktop_app import format_utils, theme
from jarvis.modules.desktop_app.daemon_worker import run_daemon_call
from jarvis.modules.desktop_app.empty_state import EmptyStateCard
from jarvis.modules.desktop_app.error_presentation import DaemonStatusBanner
from jarvis.modules.desktop_app.page_header import PageHeader
from jarvis_server.mcp_tools.models import ChunkHit, ErrorResponse, SearchResult, SourceRef

_HIT_ROLE = Qt.ItemDataRole.UserRole
_QUERY_ROLE = Qt.ItemDataRole.UserRole + 1


def _hit_fields(hit: ChunkHit) -> tuple[str, str, str]:
    """`(topic_label, document_title, preview)` - the three pieces both
    `format_hit()` and `SearchResultDelegate` render, kept in one place
    so the plain-text fallback and the rich paint can't disagree."""
    topic = hit.topic.path if hit.topic else "Unfiled"
    doc = hit.document.title or hit.document.document_id
    preview = hit.text[:200].replace("\n", " ")
    return topic, doc, preview


def _primary_source(hit: ChunkHit) -> Optional[SourceRef]:
    """The connector icon/tag/timestamp/breadcrumb (Figma parity pass,
    Phase 9) all describe one source - the first one a hit carries,
    matching the real reference's one-tag-per-result anatomy even when a
    chunk has more than one (the same content synced from two places)."""
    return hit.sources[0] if hit.sources else None


def _source_tag_label(source: SourceRef) -> Optional[str]:
    """Only Drive results get a tag. A local result's folder icon and
    breadcrumb already say where it came from, and a generic "Files" tag
    on every row would add nothing."""
    return None if source.connector == "filesystem" else "Google Drive"


def _split_preview_for_query(preview: str, query: str) -> tuple[str, str, str]:
    """`(before, matched, after)` for bolding the matched query phrase
    inline (Figma reference) - a case-insensitive substring match on the
    whole query string, the common case the reference's own examples
    show. `matched` is empty (the delegate then paints `preview` with no
    bold run) when the phrase doesn't appear verbatim - a multi-word
    query matched via FTS5/vector fusion rather than literal substring,
    or already outside the 200-char preview window."""
    if not query:
        return preview, "", ""
    idx = preview.lower().find(query.lower())
    if idx == -1:
        return preview, "", ""
    return preview[:idx], preview[idx:idx + len(query)], preview[idx + len(query):]


def format_hit(hit: ChunkHit) -> str:
    """One `ChunkHit` as a single display line - a small, pure function
    so it's directly testable without any Qt widget involved. Still set
    as each `QListWidgetItem`'s own text (search_view.py's
    `_on_search_result`) even though `SearchResultDelegate` paints
    something richer on top - this is what `.text()`/accessibility
    tooling reads."""
    topic, doc, preview = _hit_fields(hit)
    return f"[{topic}] {doc}\n{preview}"


class SearchResultDelegate(QStyledItemDelegate):
    """Paints each hit as icon + bolded title + source tag + relative
    time (one row), the preview with the matched query phrase bolded
    inline (a second row), and a breadcrumb path (a third, omitted when
    the source has none) - the real Figma reference's row anatomy
    (Figma parity pass, Phase 9), reading the original `ChunkHit` back
    from `Qt.UserRole` and the active query from `Qt.UserRole + 1` (both
    set in `_on_search_result` below) rather than re-parsing `format_hit()`'s
    formatted string. The topic tag the pre-Phase-9 version painted here
    is dropped from the visual row - the real reference shows no such
    tag, only the source one - `format_hit()`/`_hit_fields()` still
    compute it for the plain-text/accessibility string, unaffected.

    Falls back to the default `QStyledItemDelegate` paint/sizeHint for
    any item with no `ChunkHit` attached - defensive, not currently
    reachable from this view's own code, but this delegate has no way
    to know that some future caller won't add a plain item to the same
    list.
    """

    _TAG_PAD_X = 6
    _TAG_PAD_Y = 2
    _ROW_GAP = 3
    _ICON_GAP = 6
    _ICON_SIZE = 16

    def paint(self, painter, option: QStyleOptionViewItem, index: QModelIndex) -> None:
        hit = index.data(_HIT_ROLE)
        if hit is None:
            super().paint(painter, option, index)
            return

        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        hovered = bool(option.state & QStyle.StateFlag.State_MouseOver)
        _, title, preview = _hit_fields(hit)
        source = _primary_source(hit)
        query = index.data(_QUERY_ROLE) or ""

        painter.save()
        try:
            if selected:
                painter.fillRect(option.rect, QColor(theme.ACCENT))
            elif hovered:
                painter.fillRect(option.rect, QColor(theme.SURFACE_ALT))

            primary = QColor(theme.ACCENT_TEXT) if selected else QColor(theme.TEXT_PRIMARY)
            secondary = QColor(theme.ACCENT_TEXT) if selected else QColor(theme.TEXT_SECONDARY)

            x = option.rect.x() + theme.SPACING_MD
            y = option.rect.y() + theme.SPACING_SM
            width = option.rect.width() - 2 * theme.SPACING_MD

            # -- Row 1: connector icon + bolded title + source tag + relative time --
            title_font = QFont(option.font)
            title_font.setPixelSize(theme.FONT_SIZE_BASE)
            title_font.setBold(True)
            fm_title = QFontMetrics(title_font)
            row1_height = max(fm_title.height(), self._ICON_SIZE)

            text_x = x
            if source is not None:
                icon = theme.icon_from_svg(
                    theme.FOLDER_SVG if source.connector == "filesystem" else theme.CLOUD_SVG,
                    theme.ACCENT_TEXT if selected else theme.TEXT_SECONDARY,
                    self._ICON_SIZE,
                )
                icon.paint(
                    painter, x, y + (row1_height - self._ICON_SIZE) // 2,
                    self._ICON_SIZE, self._ICON_SIZE,
                )
                text_x = x + self._ICON_SIZE + self._ICON_GAP

            small_font = QFont(option.font)
            small_font.setPixelSize(theme.FONT_SIZE_SMALL)

            time_text = (
                format_utils.format_relative_time(source.source_modified_at)
                if source is not None and source.source_modified_at else ""
            )
            fm_small = QFontMetrics(small_font)
            time_width = fm_small.horizontalAdvance(time_text) if time_text else 0

            tag_text = (_source_tag_label(source) or "") if source is not None else ""
            tag_inner_width = fm_small.horizontalAdvance(tag_text) if tag_text else 0
            tag_width = (tag_inner_width + 2 * self._TAG_PAD_X) if tag_text else 0

            reserved = (
                (tag_width + self._ROW_GAP if tag_text else 0)
                + (time_width + self._ROW_GAP if time_text else 0)
            )
            title_available = max(width - (text_x - x) - reserved, 0)
            painter.setFont(title_font)
            painter.setPen(primary)
            elided_title = fm_title.elidedText(title, Qt.TextElideMode.ElideRight, title_available)
            painter.drawText(text_x, y + fm_title.ascent(), elided_title)
            title_width = fm_title.horizontalAdvance(elided_title)

            if tag_text:
                tag_rect = QRect(
                    text_x + title_width + self._ROW_GAP, y,
                    tag_width, fm_small.height() + 2 * self._TAG_PAD_Y,
                )
                if not selected:
                    painter.setPen(Qt.PenStyle.NoPen)
                    painter.setBrush(QColor(theme.SURFACE_ALT))
                    radius = tag_rect.height() / 2
                    painter.drawRoundedRect(tag_rect, radius, radius)
                painter.setFont(small_font)
                painter.setPen(secondary)
                painter.drawText(tag_rect, Qt.AlignmentFlag.AlignCenter, tag_text)

            if time_text:
                painter.setFont(small_font)
                painter.setPen(secondary)
                painter.drawText(
                    QRect(x + width - time_width, y, time_width, row1_height),
                    Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, time_text,
                )
            y += row1_height + self._ROW_GAP

            # -- Row 2: preview, with the matched query phrase bolded --
            preview_font = QFont(option.font)
            preview_font.setPixelSize(theme.FONT_SIZE_SMALL)
            fm_preview = QFontMetrics(preview_font)
            painter.setPen(secondary)
            before, match, after = _split_preview_for_query(preview, query)
            cursor_x = x
            remaining = width
            for run_text, bold in ((before, False), (match, True), (after, False)):
                if not run_text or remaining <= 0:
                    continue
                run_font = QFont(preview_font)
                run_font.setBold(bold)
                fm_run = QFontMetrics(run_font)
                elided = fm_run.elidedText(run_text, Qt.TextElideMode.ElideRight, remaining)
                painter.setFont(run_font)
                painter.drawText(cursor_x, y + fm_run.ascent(), elided)
                run_width = fm_run.horizontalAdvance(elided)
                cursor_x += run_width
                remaining -= run_width
                if elided != run_text:
                    break
            y += fm_preview.height() + self._ROW_GAP

            # -- Row 3: breadcrumb (filesystem only - Drive's source_id is opaque) --
            if source is not None and source.path:
                painter.setFont(small_font)
                painter.setPen(secondary)
                elided_path = fm_small.elidedText(
                    format_utils.tilde_path(source.path), Qt.TextElideMode.ElideMiddle, width,
                )
                painter.drawText(x, y + fm_small.ascent(), elided_path)
        finally:
            painter.restore()

    def sizeHint(self, option: QStyleOptionViewItem, index: QModelIndex) -> QSize:
        hit = index.data(_HIT_ROLE)
        if hit is None:
            return super().sizeHint(option, index)

        fm_body = QFontMetrics(option.font)
        row1_height = max(fm_body.height(), self._ICON_SIZE)
        preview_height = fm_body.height()
        source = _primary_source(hit)
        breadcrumb_height = (fm_body.height() + self._ROW_GAP) if (source is not None and source.path) else 0

        total_height = (
            theme.SPACING_SM
            + row1_height + self._ROW_GAP
            + preview_height
            + breadcrumb_height
            + theme.SPACING_SM
        )
        width = option.rect.width() if option.rect.width() > 0 else 200
        return QSize(width, int(total_height))


class SearchView(QWidget):
    # DESIGN.md §4.8's "no results" action - MainWindow wires this to a
    # page switch (no existing view can trigger one on its own; see the
    # Phase 6 plan for why this needed a new mechanism).
    go_to_sources_clicked = Signal()

    def __init__(self, pool: QThreadPool, banner: DaemonStatusBanner, parent: QWidget = None):
        super().__init__(parent)
        self._pool = pool
        self._banner = banner
        self._last_query = ""
        self._next_cursor: Optional[str] = None

        self._query_box = QLineEdit()
        self._query_box.setPlaceholderText("Search your notes, files and Drive content")
        self._query_box.returnPressed.connect(self._on_search)

        search_button = QPushButton("Search")
        search_button.clicked.connect(self._on_search)

        self._results_list = QListWidget()
        self._results_list.setItemDelegate(SearchResultDelegate(self._results_list))

        self._empty_state = QLabel("Search your notes, files and Drive content.")
        self._empty_state.setObjectName("searchEmptyState")
        self._empty_state.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._empty_state.setWordWrap(True)

        self._no_results_card = EmptyStateCard(
            "No files matched.", "Add more sources to search more of your files.",
            "Go to Sources", self.go_to_sources_clicked.emit,
        )
        self._no_results_card.hide()

        self._load_more_button = QPushButton("Load more")
        self._load_more_button.hide()
        self._load_more_button.clicked.connect(self._on_load_more)

        top_row = QHBoxLayout()
        top_row.addWidget(self._query_box, 1)
        top_row.addWidget(search_button)

        body_layout = QVBoxLayout()
        body_layout.setContentsMargins(
            theme.SPACING_XL, theme.SPACING_LG, theme.SPACING_XL, theme.SPACING_LG,
        )
        body_layout.setSpacing(theme.SPACING_LG)
        body_layout.addLayout(top_row)
        body_layout.addWidget(self._results_list, 1)
        body_layout.addWidget(self._empty_state, 1)
        body_layout.addWidget(self._no_results_card, 1)
        body_layout.addWidget(self._load_more_button, 0, Qt.AlignmentFlag.AlignHCenter)
        self._results_list.hide()

        # PageHeader spans edge-to-edge (Figma parity pass) - same split
        # as ConnectorsView's identical restructuring. No action: Search's
        # primary interaction is the query box itself, in the body.
        self._page_header = PageHeader("Search")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self._page_header)
        layout.addLayout(body_layout, 1)

    def _on_search(self) -> None:
        query = self._query_box.text().strip()
        if not query:
            return
        self._last_query = query
        self._next_cursor = None
        self._results_list.clear()
        self._load_more_button.hide()
        self._run_search(cursor=None, append=False)

    def _on_load_more(self) -> None:
        self._run_search(cursor=self._next_cursor, append=True)

    def _run_search(self, cursor: Optional[str], append: bool) -> None:
        run_daemon_call(
            self._pool,
            daemon_client.search,
            self._last_query,
            cursor=cursor,
            on_success=lambda result: self._on_search_result(result, append),
        )

    def _on_search_result(self, result, append: bool) -> None:
        if isinstance(result, ErrorResponse):
            self._banner.show_error(result.error.message)
            return
        assert isinstance(result, SearchResult)
        if not append:
            self._results_list.clear()
        for hit in result.results:
            item = QListWidgetItem(format_hit(hit))
            item.setData(_HIT_ROLE, hit)
            item.setData(_QUERY_ROLE, result.query)
            self._results_list.addItem(item)
        self._next_cursor = result.next_cursor
        self._load_more_button.setVisible(self._next_cursor is not None)

        if self._results_list.count() == 0:
            self._empty_state.hide()
            self._results_list.hide()
            self._no_results_card.show()
        else:
            self._empty_state.hide()
            self._no_results_card.hide()
            self._results_list.show()
