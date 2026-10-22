"""Hierarchy view (design doc §2b).

`get_hierarchy(root=None)` populates the top-level forest; expanding a
node the tree hasn't loaded children for yet calls `browse(topic_id)`,
which returns that node's children one level deep *and* its direct-
member chunks in the same call - so expanding a topic also populates
the chunk list for "what's directly in this topic," no second
round-trip needed.

**Deliberately `QTreeWidget`, not a custom `QAbstractItemModel`.** The
design doc suggested a custom model as "a reasonable, standard fit" for
proper lazy loading via `canFetchMore`/`fetchMore` - a real, valid
upgrade path, but more machinery than a first skeleton pass needs.
`QTreeWidget`'s item-based API gets the same lazy-on-expand behavior
(via `itemExpanded`) with far less code; swapping in a custom model
later, if real performance or richer-than-text-label item rendering
ever demands it, doesn't require rearchitecting this view's daemon-call
wiring, only its widget internals.

**Confirmed real gap, now fixed: `load_top_level()` was only ever called
once**, at app startup (`MainWindow._on_daemon_ready`) - nothing
re-triggered it afterward, so a tab left open through a later ingest
kept showing whatever existed at startup, even after the daemon
genuinely finished organizing new content. `load_top_level()` itself was
always safe to call again (it already clears `_tree` and
`_loaded_topic_ids` before rebuilding - confirmed by reading
`_on_top_level_loaded`, not assumed) - the gap was purely that nothing
after startup ever called it a second time. Fixed two ways: a manual
"Refresh" button on this view, and `MainWindow` reloading it
automatically after `ConnectorsView` reports a successful ingest (via
`ingest_succeeded`, a plain Qt signal - this view has no reference to
the connectors view and shouldn't need one).

**Polish per docs/desktop-app-ux-redesign-DESIGN.md §5/§6 (both listed
there as Medium-effort items):**

- The "Loading…" placeholder child (`_build_placeholder_item`) is
  themed - italic, `theme.TEXT_SECONDARY` - instead of plain default
  text, so it visibly reads as "in progress," not as an inert tree
  item. Set directly on the `QTreeWidgetItem` via `setFont`/
  `setForeground`, not QSS: an individual item's one-off "this specific
  row is a transient placeholder" state is exactly the kind of
  per-instance exception `theme.py`'s own docstring carves out for
  itself (QSS styles classes of widgets, not one conditionally-styled
  row inside a `QTreeWidget`) - the color value itself still comes from
  `theme.py`, not a new hardcoded one.
- "Refresh" adopts the same busy-button pattern as ingest
  (`ConnectorsView`'s `_set_controls_enabled`, design doc §4c item 5):
  disabled + relabeled "Refreshing…" for the duration of
  `load_top_level()`'s call, reverted in `_on_top_level_loaded` -
  which already runs on both the success and `ErrorResponse` branches
  (`get_hierarchy` never raises - daemon_worker.py's own docstring),
  so one revert call covers both outcomes, matching
  `ConnectorsView._stop_polling`'s "always call this regardless of
  outcome" shape.
"""

from typing import Optional

from PySide6.QtCore import Qt, QThreadPool, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QLabel,
    QListWidget,
    QSplitter,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from jarvis.modules.daemon import client as daemon_client
from jarvis.modules.desktop_app import theme
from jarvis.modules.desktop_app.daemon_worker import run_daemon_call
from jarvis.modules.desktop_app.empty_state import EmptyStateCard
from jarvis.modules.desktop_app.error_presentation import DaemonStatusBanner
from jarvis.modules.desktop_app.page_header import PageHeader
from jarvis_server.mcp_tools.models import ErrorResponse, HierarchyResult, TopicNode

_TOPIC_ID_ROLE = Qt.ItemDataRole.UserRole
_PASSAGE_PREVIEW_CHARS = 400
_SELECT_TOPIC_HINT = "Select a topic to see its passages."
_NO_PASSAGES_HINT = "This topic has no passages of its own."
_SEE_SUBTOPICS = "Open it to see its subtopics."


def _build_placeholder_item() -> QTreeWidgetItem:
    item = QTreeWidgetItem(["Loading…"])
    font = item.font(0)
    font.setItalic(True)
    item.setFont(0, font)
    item.setForeground(0, QColor(theme.TEXT_SECONDARY))
    return item


class HierarchyView(QWidget):
    # The empty state's action - MainWindow switches pages, the same way
    # SearchView's identical signal works.
    go_to_sources_clicked = Signal()

    def __init__(self, pool: QThreadPool, banner: DaemonStatusBanner, parent: QWidget = None):
        super().__init__(parent)
        self._pool = pool
        self._banner = banner
        self._loaded_topic_ids: set[str] = set()

        # Not primary: the empty state's "Go to Sources" is this page's
        # one primary action whenever there's nothing to refresh yet.
        self._page_header = PageHeader("Topics", action=("Refresh", self.load_top_level), primary=False)
        self._refresh_button = self._page_header.action_button

        self._tree = QTreeWidget()
        self._tree.setHeaderHidden(True)
        self._tree.itemExpanded.connect(self._on_item_expanded)
        self._tree.itemClicked.connect(self._on_item_clicked)

        self._chunks_list = QListWidget()
        self._chunks_list.setWordWrap(True)
        self._chunks_list.setResizeMode(QListWidget.ResizeMode.Adjust)
        self._chunks_list.hide()
        self._passages_hint = QLabel(_SELECT_TOPIC_HINT)
        self._passages_hint.setObjectName("topicsPassagesHint")
        self._passages_hint.setWordWrap(True)
        self._passages_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        passages_pane = QWidget()
        passages_layout = QVBoxLayout(passages_pane)
        passages_layout.setContentsMargins(0, 0, 0, 0)
        passages_layout.addWidget(self._passages_hint, 1)
        passages_layout.addWidget(self._chunks_list, 1)

        self._splitter = QSplitter()
        self._splitter.addWidget(self._tree)
        self._splitter.addWidget(passages_pane)
        self._splitter.setStretchFactor(1, 2)

        self._empty_state_card = EmptyStateCard(
            "Topics appear after your first sync.",
            action_text="Go to Sources", on_action=self.go_to_sources_clicked.emit,
        )
        self._empty_state_card.hide()

        body_layout = QVBoxLayout()
        body_layout.setContentsMargins(
            theme.SPACING_XL, theme.SPACING_LG, theme.SPACING_XL, theme.SPACING_LG,
        )
        body_layout.addWidget(self._splitter, 1)
        body_layout.addWidget(self._empty_state_card, 1)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self._page_header)
        layout.addLayout(body_layout, 1)

    def load_top_level(self) -> None:
        self._set_refreshing(True)
        run_daemon_call(self._pool, daemon_client.get_hierarchy, on_success=self._on_top_level_loaded)

    def _set_refreshing(self, refreshing: bool) -> None:
        self._refresh_button.setEnabled(not refreshing)
        self._refresh_button.setText("Refreshing…" if refreshing else "Refresh")

    def _on_top_level_loaded(self, result) -> None:
        self._set_refreshing(False)
        if isinstance(result, ErrorResponse):
            self._banner.show_error(result.error.message)
            return
        assert isinstance(result, HierarchyResult)
        self._tree.clear()
        self._loaded_topic_ids.clear()
        self._show_passages_hint(_SELECT_TOPIC_HINT)
        for node in result.topics:
            self._tree.addTopLevelItem(self._build_item(node))

        if result.topics:
            self._empty_state_card.hide()
            self._splitter.show()
        else:
            self._splitter.hide()
            self._empty_state_card.show()

    def _build_item(self, node: TopicNode) -> QTreeWidgetItem:
        item = QTreeWidgetItem([node.name])
        item.setData(0, _TOPIC_ID_ROLE, node.id)
        if node.children:
            for child in node.children:
                item.addChild(self._build_item(child))
        elif node.subtree_chunk_count > 0 or node.children is None:
            # A placeholder child, so Qt draws this node as expandable -
            # replaced with real children (or removed) once browse()
            # actually returns, in _on_browse_result.
            item.addChild(_build_placeholder_item())
        return item

    def _on_item_expanded(self, item: QTreeWidgetItem) -> None:
        topic_id = item.data(0, _TOPIC_ID_ROLE)
        if topic_id is None or topic_id in self._loaded_topic_ids:
            return
        self._loaded_topic_ids.add(topic_id)
        run_daemon_call(
            self._pool, daemon_client.browse, topic_id,
            on_success=lambda result, item=item: self._on_browse_result(item, result),
        )

    def _on_browse_result(self, item: QTreeWidgetItem, result) -> None:
        if isinstance(result, ErrorResponse):
            self._banner.show_error(result.error.message)
            return
        item.takeChildren()
        for child in (result.topic.children or []):
            item.addChild(self._build_item(child))

    def _on_item_clicked(self, item: QTreeWidgetItem, _column: int) -> None:
        topic_id = item.data(0, _TOPIC_ID_ROLE)
        if topic_id is None:
            return
        run_daemon_call(self._pool, daemon_client.browse, topic_id, on_success=self._on_chunks_loaded)

    def _on_chunks_loaded(self, result) -> None:
        if isinstance(result, ErrorResponse):
            self._banner.show_error(result.error.message)
            return
        self._chunks_list.clear()
        if not result.chunks:
            # No passages of its own but some beneath it means subtopics.
            has_subtopics = bool(result.topic.children) or result.topic.subtree_chunk_count > 0
            hint = f"{_NO_PASSAGES_HINT} {_SEE_SUBTOPICS}" if has_subtopics else _NO_PASSAGES_HINT
            self._show_passages_hint(hint)
            return
        for chunk in result.chunks:
            self._chunks_list.addItem(chunk.text[:_PASSAGE_PREVIEW_CHARS].replace("\n", " "))
        self._passages_hint.hide()
        self._chunks_list.show()

    def _show_passages_hint(self, text: str) -> None:
        self._chunks_list.clear()
        self._chunks_list.hide()
        self._passages_hint.setText(text)
        self._passages_hint.show()
