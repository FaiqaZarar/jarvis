"""Stats & Activity page (plan doc §9) - cumulative processing stats by
file type, and a paginated history of recent activity (Claude's and this
app's calls into the library, and topic naming during a sync). Both come
from the daemon's own `GET /stats/file_types` / `GET /stats/tool_calls`
routes, not a local file read - only the daemon process may query
`jarvis.db` (this repo's "daemon owns the db" convention, see
docs/coding-guides/database-access.md) - so every call here goes through
`run_daemon_call`, same as every other daemon-touching view.

`QTreeWidget`, not `QTableWidget`, for both tables below: `theme.py`
only styles `QListWidget`/`QTreeWidget` (the nav rail, hierarchy tree,
search results all already use one of the two), and a flat
`QTreeWidget` with no children renders identically to a table while
staying themed for free.

No filters on the tool-call history, per the plan's scope - "Load more"
is the only navigation, driven by `before_id` (the oldest `id` already
shown, per `get_recent_tool_calls`'s own cursor contract), appending
rather than replacing so earlier pages stay visible while scrolling.
"""

from typing import Optional

from PySide6.QtCore import QThreadPool
from PySide6.QtWidgets import (
    QGroupBox, QHBoxLayout, QHeaderView, QLabel, QPushButton, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from jarvis.modules.daemon import client as daemon_client
from jarvis.modules.desktop_app import format_utils, theme
from jarvis.modules.desktop_app.daemon_worker import run_daemon_call
from jarvis.modules.desktop_app.error_presentation import DaemonStatusBanner
from jarvis.modules.desktop_app.page_header import PageHeader
from jarvis_server.mcp_tools.models import ErrorResponse, FileTypeStatsResult, ToolCallHistoryResult

_TOOL_CALL_PAGE_LIMIT = 50
_NO_VALUE = "—"

# (call_type, source) as the background service records them -> what the
# row says. The raw values are internal names (DESIGN.md §5.2). Searches
# from this app and from Claude reach the same route, so the label can't
# say who asked.
_ACTIVITY_LABELS = {
    ("mcp_tool", "search"): "Searched your files",
    ("mcp_tool", "get_hierarchy"): "Opened the topic list",
    ("mcp_tool", "browse"): "Browsed a topic",
    ("mcp_tool", "related"): "Looked up related passages",
    ("mcp_tool", "get_document"): "Opened a file",
    ("mcp_tool", "query_db"): "Looked up library details",
    ("llm", "cluster_label"): "Named a topic",
}
_OTHER_ACTIVITY_LABEL = "Other activity"


def _activity_label(call_type: str, source: str) -> str:
    return _ACTIVITY_LABELS.get((call_type, source), _OTHER_ACTIVITY_LABEL)


def _flat_table(headers: list[str]) -> QTreeWidget:
    """A QTreeWidget laid out as a table: no tree indent in front of each
    row, and columns as wide as their contents, with the last one taking
    the slack, so short values like times are never cut off."""
    tree = QTreeWidget()
    tree.setHeaderLabels(headers)
    tree.setRootIsDecorated(False)
    tree.header().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
    tree.header().setStretchLastSection(True)
    return tree


class StatsActivityView(QWidget):
    def __init__(self, pool: QThreadPool, banner: DaemonStatusBanner, parent: QWidget = None):
        super().__init__(parent)
        self._pool = pool
        self._banner = banner
        self._oldest_call_id_shown: Optional[int] = None

        body_layout = QVBoxLayout()
        body_layout.setContentsMargins(
            theme.SPACING_XL, theme.SPACING_LG, theme.SPACING_XL, theme.SPACING_LG,
        )
        body_layout.addWidget(self._build_processing_stats_group())
        body_layout.addWidget(self._build_tool_call_history_group(), 1)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(PageHeader("Activity"))
        layout.addLayout(body_layout, 1)

    # -- section builders --------------------------------------------

    def _build_processing_stats_group(self) -> QGroupBox:
        group = QGroupBox("Processing stats")
        self._processing_summary_label = QLabel("")

        self._file_type_tree = _flat_table(
            ["File type", "Read", "Skipped", "Couldn't read", "Not supported", "Size"],
        )

        group_layout = QVBoxLayout(group)
        group_layout.addWidget(self._processing_summary_label)
        group_layout.addWidget(self._file_type_tree)
        return group

    def _build_tool_call_history_group(self) -> QGroupBox:
        group = QGroupBox("Recent activity")
        self._tool_calls_tree = _flat_table(["When", "What", "Result"])

        # Same wording and behaviour as Search's: shown only while there
        # may be more to load.
        self._load_more_button = QPushButton("Load more")
        self._load_more_button.hide()
        self._load_more_button.clicked.connect(self._on_load_more_clicked)

        buttons_row = QHBoxLayout()
        buttons_row.addStretch(1)
        buttons_row.addWidget(self._load_more_button)

        group_layout = QVBoxLayout(group)
        group_layout.addWidget(self._tool_calls_tree, 1)
        group_layout.addLayout(buttons_row)
        return group

    # -- visibility ------------------------------------------------------

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._load_processing_stats()
        if self._tool_calls_tree.topLevelItemCount() == 0:
            self._load_tool_call_history(cursor=None, append=False)

    # -- processing stats --------------------------------------------

    def _load_processing_stats(self) -> None:
        run_daemon_call(self._pool, daemon_client.get_file_type_stats, on_success=self._on_file_type_stats_loaded)

    def _on_file_type_stats_loaded(self, result) -> None:
        if isinstance(result, ErrorResponse):
            self._banner.show_error(result.error.message)
            return
        assert isinstance(result, FileTypeStatsResult)

        totals = result.totals
        self._processing_summary_label.setText(
            f"{totals.total_count} files processed  ·  {format_utils.format_bytes(totals.total_bytes)}"
        )

        self._file_type_tree.clear()
        for stat in result.by_extension:
            self._file_type_tree.addTopLevelItem(QTreeWidgetItem([
                stat.extension,
                str(stat.tried_count),
                str(stat.skipped_count),
                str(stat.failed_count),
                str(stat.ignored_count),
                format_utils.format_bytes(stat.bytes_processed) if stat.bytes_processed else _NO_VALUE,
            ]))

    # -- tool-call history -------------------------------------------

    def _load_tool_call_history(self, cursor: Optional[int], append: bool) -> None:
        run_daemon_call(
            self._pool, daemon_client.get_tool_call_history, cursor=cursor, limit=_TOOL_CALL_PAGE_LIMIT,
            on_success=lambda result: self._on_tool_call_history_loaded(result, append),
        )

    def _on_tool_call_history_loaded(self, result, append: bool) -> None:
        if isinstance(result, ErrorResponse):
            self._banner.show_error(result.error.message)
            return
        assert isinstance(result, ToolCallHistoryResult)

        if not append:
            self._tool_calls_tree.clear()
        for call in result.calls:
            self._tool_calls_tree.addTopLevelItem(QTreeWidgetItem([
                format_utils.format_relative_time(call.started_at),
                _activity_label(call.call_type, call.source),
                "Done" if call.success else "Failed",
            ]))
            self._oldest_call_id_shown = call.id

        self._load_more_button.setVisible(len(result.calls) == _TOOL_CALL_PAGE_LIMIT)

    def _on_load_more_clicked(self) -> None:
        self._load_tool_call_history(cursor=self._oldest_call_id_shown, append=True)
