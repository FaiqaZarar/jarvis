"""Desktop app views - basic instantiation plus the pure-function pieces
(design doc §2). Per the task: lighter than the daemon-communication
tests (Qt widget testing has real limits), but still real - each view is
actually constructed via `qtbot.addWidget` (so Qt properly parents/tears
it down) and driven through its own result-handling methods with
constructed model objects, not mocked-away entirely.
"""

import os
import threading

import pytest
from PySide6.QtCore import QSettings, QThreadPool
from PySide6.QtWidgets import QPushButton

from jarvis_server import pipeline as pipeline_mod
from jarvis_server.internal import env_config
from jarvis.modules.cloud import session_store
from jarvis.modules.desktop_app import claude_desktop_setup as cds
from jarvis.modules.desktop_app import format_utils, setup_health, theme
from jarvis.modules.desktop_app.error_presentation import DaemonStatusBanner
from jarvis.modules.desktop_app.main_window import MainWindow
from jarvis.modules.desktop_app.views.connectors_view import (
    ConnectorsView,
    _synced_roots_parent_index,
)
from jarvis.modules.desktop_app.views.hierarchy_view import HierarchyView, _build_placeholder_item
from jarvis.modules.desktop_app.views.search_view import (
    SearchResultDelegate,
    SearchView,
    _hit_fields,
    _primary_source,
    _source_tag_label,
    _split_preview_for_query,
    format_hit,
)
from jarvis.modules.desktop_app.views.settings_view import SettingsView
from jarvis.modules.desktop_app.views.stats_activity_view import StatsActivityView
from jarvis_server.mcp_tools.models import (
    BrowseResult,
    ChunkHit,
    DocumentRef,
    ErrorDetail,
    ErrorResponse,
    FileTypeStat,
    FileTypeStatsResult,
    HierarchyResult,
    ProcessingTotals,
    SearchResult,
    SourceRef,
    SyncRootRecord,
    SyncRootsResult,
    ToolCallHistoryResult,
    ToolCallRecord,
    TopicNode,
)


@pytest.fixture(autouse=True)
def _skip_first_run_settings_by_default(monkeypatch):
    """None of the tests below are about the first-run settings screen
    (that's tests/test_desktop_app_settings.py) - default every
    `MainWindow(...)` construction in this file to "a key is already
    configured" so it goes straight to the pre-existing daemon-check
    path unchanged. Without this, a real `SettingsDialog.exec()` would
    pop up and block the test indefinitely, the same class of hang
    already found and fixed once for a blocking failure dialog.

    Same reasoning, same fix shape, for the cloud sign-in dialog
    (jarvis.modules.cloud.session_store.load_session) - not about that
    either (that's test_desktop_app_sign_in.py)."""
    monkeypatch.setattr(env_config, "has_embedding_api_key", lambda: True)
    monkeypatch.setattr(session_store, "load_session", lambda: ("user@example.com", "tok"))


def _chunk_hit(text="hello world", topic_path=None, doc_title="doc.md", sources=None) -> ChunkHit:
    return ChunkHit(
        chunk_id="c1",
        text=text,
        document=DocumentRef(document_id="d1", title=doc_title),
        sources=sources if sources is not None else [],
        topic={"id": "t1", "path": topic_path} if topic_path else None,
    )


# ---------------------------------------------------------------------
# Pure functions - no Qt involved at all
# ---------------------------------------------------------------------

def test_format_hit_includes_topic_and_preview():
    hit = _chunk_hit(text="some preview text", topic_path="Coffee/Roasting")
    assert "Coffee/Roasting" in format_hit(hit)
    assert "some preview text" in format_hit(hit)


def test_format_hit_shows_unfiled_when_no_topic():
    hit = _chunk_hit(topic_path=None)
    assert "Unfiled" in format_hit(hit)


def _sync_root(connector: str, root_id: str) -> SyncRootRecord:
    return SyncRootRecord(
        connector=connector, root_id=root_id,
        first_synced_at="2026-01-01T00:00:00", last_synced_at="2026-01-01T00:00:00",
    )


def _p(*parts: str) -> str:
    """A filesystem root_id built with the host OS's real separator -
    _synced_roots_parent_index compares prefixes with `os.sep`, matching
    what a real `Path(...).resolve()` string actually looks like on this
    platform (backslash-joined on Windows). A hardcoded '/'-joined
    literal would only ever match on POSIX, silently failing this same
    assertion on Windows."""
    return os.sep + os.sep.join(parts)


def test_synced_roots_parent_index_nests_a_subfolder_under_its_parent():
    roots = [_sync_root("filesystem", _p("watched", "notes")), _sync_root("filesystem", _p("watched"))]
    assert _synced_roots_parent_index(roots) == [1, None]


def test_synced_roots_parent_index_picks_the_nearest_ancestor():
    roots = [
        _sync_root("filesystem", _p("watched", "notes", "sub")),
        _sync_root("filesystem", _p("watched")),
        _sync_root("filesystem", _p("watched", "notes")),
    ]
    assert _synced_roots_parent_index(roots) == [2, None, 1]


def test_synced_roots_parent_index_does_not_nest_a_sibling_with_a_shared_prefix():
    roots = [
        _sync_root("filesystem", _p("watched", "notes-archive")),
        _sync_root("filesystem", _p("watched", "notes")),
    ]
    assert _synced_roots_parent_index(roots) == [None, None]


def test_synced_roots_parent_index_never_nests_across_connectors():
    roots = [_sync_root("drive", "folder-abc"), _sync_root("filesystem", _p("watched"))]
    assert _synced_roots_parent_index(roots) == [None, None]


def test_hit_fields_matches_format_hit():
    """format_hit() and SearchResultDelegate must render from the exact
    same three pieces, not two independently-derived copies that could
    drift apart."""
    hit = _chunk_hit(text="some preview text", topic_path="Coffee/Roasting", doc_title="notes.md")
    topic, title, preview = _hit_fields(hit)
    rendered = format_hit(hit)
    assert topic == "Coffee/Roasting"
    assert title == "notes.md"
    assert preview == "some preview text"
    assert topic in rendered and title in rendered and preview in rendered


def _source(connector="filesystem", **kwargs) -> SourceRef:
    defaults = dict(source_id="s1", connector=connector, last_synced_at="2026-01-01T00:00:00")
    defaults.update(kwargs)
    return SourceRef(**defaults)


def test_primary_source_is_the_first_one_or_none():
    assert _primary_source(_chunk_hit(sources=[])) is None
    src = _source()
    assert _primary_source(_chunk_hit(sources=[src])) is src


def test_source_tag_label_tags_only_drive_results():
    assert _source_tag_label(_source(connector="filesystem")) is None
    assert _source_tag_label(_source(connector="drive")) == "Google Drive"


def test_split_preview_for_query_finds_a_verbatim_case_insensitive_match():
    before, match, after = _split_preview_for_query("the Quarterly Budget report", "quarterly budget")
    assert before == "the "
    assert match == "Quarterly Budget"
    assert after == " report"


def test_split_preview_for_query_no_match_returns_everything_unbolded():
    before, match, after = _split_preview_for_query("nothing relevant here", "quarterly budget")
    assert before == "nothing relevant here"
    assert match == ""
    assert after == ""


def test_split_preview_for_query_empty_query_returns_everything_unbolded():
    before, match, after = _split_preview_for_query("some preview", "")
    assert before == "some preview"
    assert match == ""
    assert after == ""


# ---------------------------------------------------------------------
# Basic instantiation
# ---------------------------------------------------------------------

def test_search_view_instantiates(qtbot):
    banner = DaemonStatusBanner()
    view = SearchView(QThreadPool(), banner)
    qtbot.addWidget(view)

    assert view._load_more_button.isHidden() is True
    assert view._query_box.placeholderText() == "Search your notes, files and Drive content"


def test_search_view_results_list_uses_the_result_delegate(qtbot):
    """Confirmed real wiring, not just "the delegate class exists" -
    format_hit()/SearchResultDelegate only actually change what a user
    sees if the list is genuinely using this delegate instead of Qt's
    own default."""
    banner = DaemonStatusBanner()
    view = SearchView(QThreadPool(), banner)
    qtbot.addWidget(view)

    assert isinstance(view._results_list.itemDelegate(), SearchResultDelegate)


def test_search_result_delegate_size_hint_is_taller_than_default_for_a_hit(qtbot):
    """The delegate needs room for three stacked rows (tag/title/preview)
    - a real regression here would be the sizeHint silently falling back
    to a single default line, clipping two of the three rows."""
    from PySide6.QtCore import QModelIndex
    from PySide6.QtWidgets import QStyleOptionViewItem

    banner = DaemonStatusBanner()
    view = SearchView(QThreadPool(), banner)
    qtbot.addWidget(view)
    view._on_search_result(SearchResult(query="q", results=[_chunk_hit()], next_cursor=None), append=False)

    delegate = view._results_list.itemDelegate()
    option = QStyleOptionViewItem()
    option.rect = view._results_list.visualItemRect(view._results_list.item(0))
    index = view._results_list.model().index(0, 0)

    default_option = QStyleOptionViewItem()
    default_option.rect = option.rect
    default_hint = super(SearchResultDelegate, delegate).sizeHint(default_option, QModelIndex())

    hit_hint = delegate.sizeHint(option, index)
    assert hit_hint.height() > default_hint.height()


def test_search_result_delegate_size_hint_grows_for_a_breadcrumb_line(qtbot):
    """A filesystem hit (real path, breadcrumb shown) needs a taller row
    than one with no source at all (no breadcrumb line)."""
    from PySide6.QtWidgets import QStyleOptionViewItem

    banner = DaemonStatusBanner()
    view = SearchView(QThreadPool(), banner)
    qtbot.addWidget(view)
    no_source_hit = _chunk_hit(sources=[])
    with_path_hit = _chunk_hit(sources=[_source(connector="filesystem", path="/tmp/doc.md")])
    view._on_search_result(
        SearchResult(query="q", results=[no_source_hit, with_path_hit], next_cursor=None), append=False,
    )

    delegate = view._results_list.itemDelegate()
    option = QStyleOptionViewItem()
    option.rect = view._results_list.visualItemRect(view._results_list.item(0))

    no_breadcrumb_hint = delegate.sizeHint(option, view._results_list.model().index(0, 0))
    with_breadcrumb_hint = delegate.sizeHint(option, view._results_list.model().index(1, 0))
    assert with_breadcrumb_hint.height() > no_breadcrumb_hint.height()


def test_search_result_delegate_paint_does_not_raise_with_a_full_row(qtbot):
    """sizeHint tests don't exercise paint()'s own QPainter calls at all
    - a real smoke test against a hit carrying every optional piece
    (icon, tag, timestamp, bolded preview match, breadcrumb) at once,
    the combination most likely to trip an off-by-one in the manual
    multi-run text layout."""
    from PySide6.QtGui import QPixmap, QPainter
    from PySide6.QtWidgets import QStyleOptionViewItem

    banner = DaemonStatusBanner()
    view = SearchView(QThreadPool(), banner)
    qtbot.addWidget(view)
    hit = _chunk_hit(
        text="Updated projections for the quarterly budget across departments",
        sources=[_source(
            connector="filesystem", path="/tmp/Finance/budget.md",
            source_modified_at="2026-01-01T00:00:00",
        )],
    )
    view._on_search_result(SearchResult(query="quarterly budget", results=[hit], next_cursor=None), append=False)

    delegate = view._results_list.itemDelegate()
    option = QStyleOptionViewItem()
    option.rect = view._results_list.visualItemRect(view._results_list.item(0))
    option.rect.setWidth(400)
    index = view._results_list.model().index(0, 0)

    pixmap = QPixmap(400, 100)
    painter = QPainter(pixmap)
    try:
        delegate.paint(painter, option, index)  # must not raise
    finally:
        painter.end()


def test_on_search_result_stores_the_query_for_the_delegate_to_read(qtbot):
    banner = DaemonStatusBanner()
    view = SearchView(QThreadPool(), banner)
    qtbot.addWidget(view)

    view._on_search_result(SearchResult(query="quarterly budget", results=[_chunk_hit()], next_cursor=None), append=False)

    from jarvis.modules.desktop_app.views.search_view import _QUERY_ROLE
    item = view._results_list.item(0)
    assert item.data(_QUERY_ROLE) == "quarterly budget"


def test_hierarchy_view_instantiates(qtbot):
    banner = DaemonStatusBanner()
    view = HierarchyView(QThreadPool(), banner)
    qtbot.addWidget(view)

    assert view._tree.topLevelItemCount() == 0


def test_hierarchy_view_loading_placeholder_is_themed_not_plain_text(qtbot):
    """docs/desktop-app-ux-redesign-DESIGN.md §5's specific ask - italic,
    theme.TEXT_SECONDARY - not just "some" styling."""
    item = _build_placeholder_item()

    assert item.text(0) == "Loading…"
    assert item.font(0).italic() is True
    assert item.foreground(0).color().name() == theme.TEXT_SECONDARY.lower()


def test_connectors_view_instantiates(qtbot):
    view = ConnectorsView(QThreadPool())
    qtbot.addWidget(view)


def test_main_window_instantiates_with_tabs_disabled_before_daemon_check_resolves(qtbot, monkeypatch):
    # Prevent the real auto-start check from ever firing during this
    # instantiation test - it's covered on its own in
    # test_desktop_app_daemon_worker.py / the daemon test suite;
    # here only the widget-construction shape is being checked.
    from jarvis.modules.desktop_app import main_window as main_window_mod

    monkeypatch.setattr(main_window_mod, "run_daemon_call", lambda *a, **k: None)

    window = MainWindow(QThreadPool())
    qtbot.addWidget(window)

    assert window.pages.count() == 5
    for view in (window.search_view, window.hierarchy_view, window.connectors_view):
        assert view.isEnabled() is False
    assert window.settings_view.isEnabled() is True
    assert window.stats_view.isEnabled() is True
    assert window.banner.isVisible() is False


# ---------------------------------------------------------------------
# Window geometry persistence (docs/desktop-app-ux-redesign-DESIGN.md §7).
# QSettings redirected to a temp .ini file for the duration of each test -
# real QSettings() would otherwise read/write the real Windows registry
# (or the real user's config file on Mac/Linux) under this app's real
# organization/application name.
# ---------------------------------------------------------------------

@pytest.fixture
def isolated_settings(tmp_path, monkeypatch):
    from jarvis.modules.desktop_app import main_window as main_window_mod

    ini_path = tmp_path / "test_settings.ini"
    monkeypatch.setattr(
        main_window_mod, "_settings",
        lambda: QSettings(str(ini_path), QSettings.Format.IniFormat),
    )
    return ini_path


def test_main_window_close_saves_its_current_geometry(qtbot, monkeypatch, isolated_settings):
    from jarvis.modules.desktop_app import main_window as main_window_mod

    monkeypatch.setattr(main_window_mod, "run_daemon_call", lambda *a, **k: None)

    window = MainWindow(QThreadPool())
    qtbot.addWidget(window)
    expected = window.saveGeometry()
    window.close()

    assert main_window_mod._settings().value("MainWindow/geometry") == expected


def test_main_window_restores_a_previously_saved_geometry(qtbot, monkeypatch, isolated_settings):
    """Checks the plumbing (the persisted bytes reach `restoreGeometry()`
    unchanged), not the resulting on-screen geometry - comparing
    `saveGeometry()` between a shown and a not-yet-shown window is real
    window-manager state that isn't byte-stable across platforms/backends,
    even after a correct `restoreGeometry()` call."""
    from jarvis.modules.desktop_app import main_window as main_window_mod

    monkeypatch.setattr(main_window_mod, "run_daemon_call", lambda *a, **k: None)

    seed = MainWindow(QThreadPool())
    qtbot.addWidget(seed)
    seed.show()
    qtbot.waitExposed(seed)
    seed.resize(950, 700)
    seed.move(120, 80)
    saved_geometry = seed.saveGeometry()
    main_window_mod._settings().setValue("MainWindow/geometry", saved_geometry)

    restored_with = []
    monkeypatch.setattr(
        MainWindow, "restoreGeometry",
        lambda self, geometry: restored_with.append(bytes(geometry)) or True,
    )

    window = MainWindow(QThreadPool())
    qtbot.addWidget(window)

    assert restored_with == [bytes(saved_geometry)]


def test_main_window_uses_default_geometry_when_nothing_was_saved_yet(qtbot, monkeypatch, isolated_settings):
    from jarvis.modules.desktop_app import main_window as main_window_mod

    monkeypatch.setattr(main_window_mod, "run_daemon_call", lambda *a, **k: None)

    # No prior close() happened - isolated_settings' .ini file doesn't
    # even exist yet - confirms restoreGeometry() is never called on a
    # None value (QSettings.value() returns None for a missing key).
    window = MainWindow(QThreadPool())
    qtbot.addWidget(window)

    assert window.isVisible() is False  # never shown - didn't crash either


# ---------------------------------------------------------------------
# Result handling, driven directly with constructed model objects -
# real view logic, without needing a real daemon call or worker thread.
# ---------------------------------------------------------------------

def test_search_view_populates_results_and_enables_load_more_when_cursor_present(qtbot):
    banner = DaemonStatusBanner()
    view = SearchView(QThreadPool(), banner)
    qtbot.addWidget(view)

    result = SearchResult(query="q", results=[_chunk_hit(text="a"), _chunk_hit(text="b")], next_cursor="cursor-2")
    view._on_search_result(result, append=False)

    assert view._results_list.count() == 2
    assert view._load_more_button.isHidden() is False
    assert view._next_cursor == "cursor-2"


def test_search_view_hides_load_more_when_no_next_cursor(qtbot):
    banner = DaemonStatusBanner()
    view = SearchView(QThreadPool(), banner)
    qtbot.addWidget(view)

    view._on_search_result(SearchResult(query="q", results=[_chunk_hit()], next_cursor=None), append=False)

    assert view._load_more_button.isHidden() is True


def test_search_view_shows_banner_on_error_response(qtbot):
    banner = DaemonStatusBanner()
    view = SearchView(QThreadPool(), banner)
    qtbot.addWidget(view)
    qtbot.addWidget(banner)

    view._on_search_result(ErrorResponse(error=ErrorDetail(code="daemon_unavailable", message="nope")), append=False)

    assert banner.isVisible() is True


def test_search_view_appends_results_instead_of_replacing_on_load_more(qtbot):
    banner = DaemonStatusBanner()
    view = SearchView(QThreadPool(), banner)
    qtbot.addWidget(view)

    view._on_search_result(SearchResult(query="q", results=[_chunk_hit(text="a")], next_cursor="c2"), append=False)
    view._on_search_result(SearchResult(query="q", results=[_chunk_hit(text="b")], next_cursor=None), append=True)

    assert view._results_list.count() == 2


# ---------------------------------------------------------------------
# Empty states (DESIGN.md §4.8, Phase 6) - EmptyStateCard's own
# rendering is covered by test_desktop_app_empty_state.py; these confirm
# SearchView/HierarchyView actually show/hide it at the right moments.
# ---------------------------------------------------------------------

def test_search_view_shows_empty_state_card_on_zero_results(qtbot):
    banner = DaemonStatusBanner()
    view = SearchView(QThreadPool(), banner)
    qtbot.addWidget(view)

    view._on_search_result(SearchResult(query="q", results=[], next_cursor=None), append=False)

    assert view._no_results_card.isHidden() is False
    assert view._results_list.isHidden() is True


def test_search_view_hides_empty_state_card_once_results_exist(qtbot):
    banner = DaemonStatusBanner()
    view = SearchView(QThreadPool(), banner)
    qtbot.addWidget(view)
    view._on_search_result(SearchResult(query="q", results=[], next_cursor=None), append=False)

    view._on_search_result(SearchResult(query="q", results=[_chunk_hit()], next_cursor=None), append=False)

    assert view._no_results_card.isHidden() is True
    assert view._results_list.isHidden() is False


def test_search_view_go_to_sources_card_action_emits_signal(qtbot):
    banner = DaemonStatusBanner()
    view = SearchView(QThreadPool(), banner)
    qtbot.addWidget(view)
    view._on_search_result(SearchResult(query="q", results=[], next_cursor=None), append=False)
    seen = []
    view.go_to_sources_clicked.connect(lambda: seen.append(True))

    view._no_results_card.findChild(QPushButton).click()

    assert seen == [True]


def test_hierarchy_view_builds_top_level_tree_items(qtbot):
    banner = DaemonStatusBanner()
    view = HierarchyView(QThreadPool(), banner)
    qtbot.addWidget(view)

    result = HierarchyResult(
        topics=[
            TopicNode(id="t1", name="Coffee", path="/Coffee", level=0, direct_chunk_count=0, subtree_chunk_count=5),
            TopicNode(id="t2", name="SQLite", path="/SQLite", level=0, direct_chunk_count=2, subtree_chunk_count=2),
        ],
    )
    view._on_top_level_loaded(result)

    assert view._tree.topLevelItemCount() == 2
    assert view._tree.topLevelItem(0).text(0) == "Coffee"
    assert view._empty_state_card.isHidden() is True
    assert view._splitter.isHidden() is False


def test_hierarchy_view_shows_empty_state_card_on_zero_topics(qtbot):
    banner = DaemonStatusBanner()
    view = HierarchyView(QThreadPool(), banner)
    qtbot.addWidget(view)

    view._on_top_level_loaded(HierarchyResult(topics=[]))

    assert view._empty_state_card.isHidden() is False
    assert view._splitter.isHidden() is True


def test_hierarchy_view_hides_empty_state_card_once_topics_exist(qtbot):
    banner = DaemonStatusBanner()
    view = HierarchyView(QThreadPool(), banner)
    qtbot.addWidget(view)
    view._on_top_level_loaded(HierarchyResult(topics=[]))

    view._on_top_level_loaded(HierarchyResult(topics=[_topic("t1", "Coffee")]))

    assert view._empty_state_card.isHidden() is True
    assert view._splitter.isHidden() is False


def test_hierarchy_view_shows_banner_on_error_response(qtbot):
    banner = DaemonStatusBanner()
    view = HierarchyView(QThreadPool(), banner)
    qtbot.addWidget(view)
    qtbot.addWidget(banner)

    view._on_top_level_loaded(ErrorResponse(error=ErrorDetail(code="daemon_unavailable", message="nope")))

    assert banner.isVisible() is True


def test_hierarchy_view_populates_chunks_list_on_browse_result(qtbot):
    banner = DaemonStatusBanner()
    view = HierarchyView(QThreadPool(), banner)
    qtbot.addWidget(view)

    node = TopicNode(id="t1", name="Coffee", path="/Coffee", level=0)
    result = BrowseResult(topic=node, chunks=[_chunk_hit(text="in coffee")], next_cursor=None)
    view._on_chunks_loaded(result)

    assert view._chunks_list.count() == 1
    assert view._chunks_list.isHidden() is False
    assert view._passages_hint.isHidden() is True


def test_hierarchy_view_has_a_page_header_and_no_redundant_column_header(qtbot):
    view = HierarchyView(QThreadPool(), DaemonStatusBanner())
    qtbot.addWidget(view)

    assert view._page_header._title_label.text() == "Topics"
    assert view._page_header.action_button is view._refresh_button
    assert view._refresh_button.property("buttonKind") in (None, "")
    assert view._tree.isHeaderHidden() is True


def test_hierarchy_view_asks_to_select_a_topic_until_one_is_opened(qtbot):
    view = HierarchyView(QThreadPool(), DaemonStatusBanner())
    qtbot.addWidget(view)
    view._on_top_level_loaded(HierarchyResult(topics=[_topic("t1", "Coffee")]))

    assert view._passages_hint.isHidden() is False
    assert view._passages_hint.text() == "Select a topic to see its passages."
    assert view._chunks_list.isHidden() is True


def test_hierarchy_view_says_when_a_topic_has_no_passages_of_its_own(qtbot):
    view = HierarchyView(QThreadPool(), DaemonStatusBanner())
    qtbot.addWidget(view)
    parent = TopicNode(
        id="t1", name="Coffee", path="/Coffee", level=0, subtree_chunk_count=4,
        children=[TopicNode(id="t2", name="Roasting", path="/Coffee/Roasting", level=1)],
    )

    view._on_chunks_loaded(BrowseResult(topic=parent, chunks=[], next_cursor=None))

    assert view._passages_hint.isHidden() is False
    assert view._passages_hint.text() == "This topic has no passages of its own. Open it to see its subtopics."


def test_hierarchy_view_does_not_point_to_subtopics_a_topic_lacks(qtbot):
    view = HierarchyView(QThreadPool(), DaemonStatusBanner())
    qtbot.addWidget(view)
    leaf = TopicNode(id="t1", name="Coffee", path="/Coffee", level=0, children=[])

    view._on_chunks_loaded(BrowseResult(topic=leaf, chunks=[], next_cursor=None))

    assert view._passages_hint.text() == "This topic has no passages of its own."


def test_hierarchy_view_wraps_passages_instead_of_cutting_them_off(qtbot):
    view = HierarchyView(QThreadPool(), DaemonStatusBanner())
    qtbot.addWidget(view)

    assert view._chunks_list.wordWrap() is True


def test_hierarchy_view_empty_card_offers_go_to_sources(qtbot):
    view = HierarchyView(QThreadPool(), DaemonStatusBanner())
    qtbot.addWidget(view)
    view._on_top_level_loaded(HierarchyResult(topics=[]))
    seen = []
    view.go_to_sources_clicked.connect(lambda: seen.append(True))

    view._empty_state_card.findChild(QPushButton).click()

    assert seen == [True]


def test_activity_and_settings_have_page_headers(qtbot):
    from jarvis.modules.desktop_app.page_header import PageHeader

    activity = StatsActivityView(QThreadPool(), DaemonStatusBanner())
    settings = SettingsView(QThreadPool(), DaemonStatusBanner(), lambda: None)
    qtbot.addWidget(activity)
    qtbot.addWidget(settings)

    assert activity.findChild(PageHeader)._title_label.text() == "Activity"
    assert settings.findChild(PageHeader)._title_label.text() == "Settings"


# ---------------------------------------------------------------------
# MainWindow's real auto-start wiring (design doc §4) - the actual
# ensure_daemon_running() call, run through the real worker/signal path,
# just with the daemon-reachability outcome itself mocked so this test
# doesn't depend on a real daemon process existing.
# ---------------------------------------------------------------------

def test_main_window_enables_tabs_when_daemon_becomes_ready(qtbot, monkeypatch):
    from jarvis.modules.desktop_app import main_window as main_window_mod
    from jarvis.modules.desktop_app.views import hierarchy_view as hierarchy_view_mod

    monkeypatch.setattr(main_window_mod, "ensure_daemon_running", lambda: 54321)
    # _on_daemon_ready also registers as a daemon client for the orphan-
    # shutdown fix - real, unmocked, it would try a genuine network call
    # (and daemon auto-spawn) against port 54321, a fake port nothing is
    # listening on. Mocked here for the same reason ensure_daemon_running
    # already is above.
    monkeypatch.setattr(main_window_mod, "register_client", lambda: "fake-client-id")
    # _on_daemon_ready also calls hierarchy_view.load_top_level(), which
    # goes straight to daemon_client.get_hierarchy() - a different,
    # module-qualified reference than main_window_mod's own copy above,
    # so mocking that doesn't cover this call too (the same real-daemon-
    # spawn risk, just reached through a different view).
    monkeypatch.setattr(hierarchy_view_mod.daemon_client, "get_hierarchy", lambda: HierarchyResult(topics=[]))

    window = MainWindow(QThreadPool())
    qtbot.addWidget(window)

    qtbot.waitUntil(lambda: window.search_view.isEnabled(), timeout=2000)
    assert window.hierarchy_view.isEnabled() is True
    assert window.connectors_view.isEnabled() is True
    assert window.banner.isVisible() is False


def test_main_window_shows_banner_when_daemon_unreachable(qtbot, monkeypatch):
    from jarvis.modules.daemon.client import DaemonStartupError
    from jarvis.modules.desktop_app import main_window as main_window_mod

    def _boom():
        raise DaemonStartupError("The Jarvis background service didn't start in time. Try restarting the app.")

    monkeypatch.setattr(main_window_mod, "ensure_daemon_running", _boom)

    window = MainWindow(QThreadPool())
    qtbot.addWidget(window)

    # isHidden(), not isVisible(): the banner is a child of this
    # not-yet-shown MainWindow, so isVisible() (which depends on the
    # whole ancestor chain actually being on screen) would report False
    # regardless of whether show_error() ran - isHidden() reflects the
    # widget's own show()/hide() state directly, which is what
    # DaemonStatusBanner.show_error/hide actually toggle. A real,
    # shown window (app.py's actual main()) doesn't have this wrinkle.
    qtbot.waitUntil(lambda: not window.banner.isHidden(), timeout=2000)
    assert window.search_view.isEnabled() is False
    # The GUI shows its own calm DESIGN.md §6 wording here, deliberately
    # not daemon_client's raw exception text (see _on_daemon_unreachable)
    # - that text is still shared with the CLI tools, unaffected.
    assert window.banner.message() == "The background service isn't running. Everything already saved is safe."
    # DESIGN.md §4.2: an error stays until it's resolved.
    assert window.banner._dismiss_button.isHidden()
    assert window.banner._action_button.text() == "Restart service"


def test_restart_service_on_the_banner_restarts_it_instead_of_opening_settings(qtbot, monkeypatch):
    from jarvis.modules.daemon.client import DaemonStartupError
    from jarvis.modules.desktop_app import main_window as main_window_mod

    def _boom():
        raise DaemonStartupError("not running")

    restarts = []
    monkeypatch.setattr(main_window_mod, "ensure_daemon_running", _boom)
    monkeypatch.setattr(main_window_mod.process_stats, "force_restart_daemon", lambda: restarts.append(True))
    monkeypatch.setattr(main_window_mod, "register_client", lambda: "client-1")

    window = MainWindow(QThreadPool())
    qtbot.addWidget(window)
    qtbot.waitUntil(lambda: not window.banner.isHidden(), timeout=2000)

    window.banner._action_button.click()

    qtbot.waitUntil(lambda: window.search_view.isEnabled(), timeout=2000)
    assert restarts == [True]
    assert window.pages.currentIndex() == 0  # still on Sources, not sent to Settings


def test_passing_banners_can_still_be_dismissed(qtbot):
    banner = DaemonStatusBanner()
    qtbot.addWidget(banner)

    banner.show_error("Not running.", dismissible=False)
    banner.show_success("You're up to date.")

    assert not banner._dismiss_button.isHidden()


def test_main_window_recovers_when_daemon_comes_back_on_its_own(qtbot, monkeypatch):
    """Confirmed real gap: previously nothing re-checked after the
    initial ensure_daemon_running() gave up, so a daemon that came up on
    its own moments later (a slow cold start, not an actual failure) left
    the banner stuck regardless - a passive recheck timer now watches for
    exactly this. It must use is_daemon_reachable(), never ensure_daemon_
    running() again, so it never attempts a second spawn on every tick
    while genuinely down - confirmed here by never making ensure_daemon_
    running succeed, only the passive check."""
    from jarvis.modules.daemon.client import DaemonStartupError
    from jarvis.modules.desktop_app import main_window as main_window_mod
    from jarvis.modules.desktop_app.views import hierarchy_view as hierarchy_view_mod

    def _boom():
        raise DaemonStartupError("The Jarvis background service didn't start in time. Try restarting the app.")

    monkeypatch.setattr(main_window_mod, "ensure_daemon_running", _boom)
    monkeypatch.setattr(main_window_mod, "register_client", lambda: "fake-client-id")
    monkeypatch.setattr(hierarchy_view_mod.daemon_client, "get_hierarchy", lambda: HierarchyResult(topics=[]))

    window = MainWindow(QThreadPool())
    qtbot.addWidget(window)

    qtbot.waitUntil(lambda: not window.banner.isHidden(), timeout=2000)
    assert window._daemon_recheck_timer.isActive() is True

    monkeypatch.setattr(main_window_mod, "is_daemon_reachable", lambda: True)
    window._recheck_daemon()

    qtbot.waitUntil(lambda: window.search_view.isEnabled(), timeout=2000)
    # dismiss() fades the banner out over 180ms before actually hiding it
    # (error_presentation.py's _FADE_DURATION_MS) - isEnabled() above
    # flips synchronously, well before that animation finishes.
    qtbot.waitUntil(lambda: window.banner.isHidden(), timeout=1000)
    assert window._daemon_recheck_timer.isActive() is False


def test_main_window_recheck_keeps_waiting_silently_while_still_down(qtbot, monkeypatch):
    """The other half: while genuinely still down, a tick must not
    re-show the banner, re-raise, or do anything visible - just wait for
    the next tick."""
    from jarvis.modules.daemon.client import DaemonStartupError
    from jarvis.modules.desktop_app import main_window as main_window_mod

    def _boom():
        raise DaemonStartupError("The Jarvis background service didn't start in time. Try restarting the app.")

    monkeypatch.setattr(main_window_mod, "ensure_daemon_running", _boom)

    window = MainWindow(QThreadPool())
    qtbot.addWidget(window)

    qtbot.waitUntil(lambda: not window.banner.isHidden(), timeout=2000)

    monkeypatch.setattr(main_window_mod, "is_daemon_reachable", lambda: False)
    window._recheck_daemon()
    qtbot.wait(100)

    assert window.banner.isHidden() is False
    assert window.search_view.isEnabled() is False
    assert window._daemon_recheck_timer.isActive() is True


def test_main_window_retires_a_registration_that_completes_after_close(qtbot, monkeypatch):
    """Real, confirmed race: ensure_daemon_running() can take several
    seconds on a cold start, and register_client() only fires after it
    succeeds - so a user can close the window before _daemon_client_id
    is ever set. closeEvent() finding None there used to mean nothing
    would ever retire that registration once it did complete - the exact
    orphaned-daemon bug this whole mechanism exists to prevent, via a
    narrower door. Drives the two halves directly (not through real
    threading/timing) since the race itself isn't what's under test -
    the _closing flag's handling of it is."""
    from jarvis.modules.desktop_app import main_window as main_window_mod

    monkeypatch.setattr(main_window_mod, "run_daemon_call", lambda *a, **k: None)
    retired = []
    monkeypatch.setattr(main_window_mod, "request_shutdown", lambda client_id: retired.append(client_id))

    window = MainWindow(QThreadPool())
    qtbot.addWidget(window)

    window.close()  # _daemon_client_id is still None here - nothing to retire yet
    assert retired == []

    # Registration completes only now, after the window is already gone.
    window._on_client_registered("late-client-id")

    assert retired == ["late-client-id"]


def test_connectors_view_shows_a_success_banner_on_ingest_success(qtbot):
    view = ConnectorsView(QThreadPool())
    qtbot.addWidget(view)

    view._on_ingest_result({
        "sync": {"pulled": 1, "fast_skipped": 0, "skipped": 0, "errors": 0},
        "chunks_created": 2,
        "embed": {},
        "hierarchy": {"skipped": "organize_hierarchy=False"},
    })

    assert "All caught up. 1 file saved" in view._status_banner.message()


def test_connectors_view_renders_synced_roots_list_with_nesting(qtbot):
    view = ConnectorsView(QThreadPool())
    qtbot.addWidget(view)

    watched = _p("watched")
    watched_notes = _p("watched", "notes")
    view._on_synced_roots_loaded(SyncRootsResult(roots=[
        _sync_root("filesystem", watched_notes),
        _sync_root("filesystem", watched),
        _sync_root("drive", "folder-abc"),
    ]))

    layout = view._synced_roots_rows_layout
    ordered = [layout.itemAt(i).widget().root_id for i in range(len(view._synced_root_rows))]
    # alphabetical by display name, notes nested right under /watched
    assert ordered == ["folder-abc", watched, watched_notes]
    assert view._synced_root_rows[watched_notes].layout().contentsMargins().left() > 0
    assert view._synced_root_rows[watched].layout().contentsMargins().left() == 0
    from jarvis.modules.desktop_app.views.connectors_view import SyncedRootRow
    assert isinstance(view._synced_root_rows[watched], SyncedRootRow)
    assert isinstance(view._synced_root_rows[watched_notes], SyncedRootRow)


def test_connectors_view_shows_banner_on_synced_roots_error(qtbot):
    view = ConnectorsView(QThreadPool())
    qtbot.addWidget(view)

    view._on_synced_roots_loaded(ErrorResponse(error=ErrorDetail(code="daemon_unavailable", message="offline")))

    assert view._status_banner.message() == "offline"


# ---------------------------------------------------------------------
# Sources row redesign wiring (DESIGN.md §4.6, Phase 5) - SyncedRootRow's
# own rendering is covered by test_desktop_app_sources_row.py; these
# confirm ConnectorsView actually drives it correctly end to end.
# ---------------------------------------------------------------------

def test_syncing_row_shows_cancel_and_floats_to_the_top(qtbot):
    view = ConnectorsView(QThreadPool())
    qtbot.addWidget(view)
    zebra, apple = _sync_root("filesystem", "zebra"), _sync_root("filesystem", "apple")
    view._on_synced_roots_loaded(SyncRootsResult(roots=[zebra, apple]))

    view._source_name = "zebra"
    view._poll_timer.start()
    view._on_synced_roots_loaded(SyncRootsResult(roots=[zebra, apple]))

    assert view._synced_roots_rows_layout.itemAt(0).widget().root_id == "zebra"
    assert view._synced_root_rows["zebra"]._action_button.text() == "Cancel"
    view._poll_timer.stop()


def test_busy_disables_other_rows_but_not_the_syncing_row(qtbot):
    view = ConnectorsView(QThreadPool())
    qtbot.addWidget(view)
    zebra, apple = _sync_root("filesystem", "zebra"), _sync_root("filesystem", "apple")
    view._on_synced_roots_loaded(SyncRootsResult(roots=[zebra, apple]))

    view._source_name = "zebra"
    view._poll_timer.start()
    view._refresh_row_states()

    assert view._synced_root_rows["apple"]._action_button.isEnabled() is False
    assert view._synced_root_rows["apple"]._action_button.text() == "Sync now"
    assert view._synced_root_rows["zebra"]._action_button.isEnabled() is True
    view._poll_timer.stop()


def test_synced_root_row_action_says_sync_now_not_resync(qtbot):
    view = ConnectorsView(QThreadPool())
    qtbot.addWidget(view)

    view._on_synced_roots_loaded(SyncRootsResult(roots=[_sync_root("filesystem", "watched")]))

    assert view._synced_root_rows["watched"]._action_button.text() == "Sync now"


def test_stopping_polling_re_enables_every_row(qtbot):
    view = ConnectorsView(QThreadPool())
    qtbot.addWidget(view)
    zebra, apple = _sync_root("filesystem", "zebra"), _sync_root("filesystem", "apple")
    view._on_synced_roots_loaded(SyncRootsResult(roots=[zebra, apple]))
    view._source_name = "zebra"
    view._poll_timer.start()
    view._refresh_row_states()

    view._stop_polling()

    assert view._synced_root_rows["apple"]._action_button.isEnabled() is True
    assert view._synced_root_rows["zebra"]._action_button.text() == "Sync now"


# ---------------------------------------------------------------------
# Setup checklist wiring (DESIGN.md §4.7) - variant selection, "Not now"/
# dismiss actions, and the Quit-reopens-the-real-sign-in-gate case.
# session_store.load_session is already monkeypatched signed-in by the
# autouse fixture above; these tests only vary has_source/claude_desktop.
# ---------------------------------------------------------------------

def _claude_desktop_plan(case, existing_jarvis_entry=None):
    return cds.ConnectClaudeDesktopPlan(
        config_path=None, case=case, instructions="", snippet=None,
        existing_jarvis_entry=existing_jarvis_entry,
    )


def test_connectors_view_shows_card_and_hides_group_when_zero_sources(qtbot, monkeypatch):
    from jarvis.modules.desktop_app.views import connectors_view as connectors_view_mod

    monkeypatch.setattr(connectors_view_mod.cds, "build_plan", lambda: _claude_desktop_plan(cds.CASE_MISSING))

    view = ConnectorsView(QThreadPool())
    qtbot.addWidget(view)

    view._on_synced_roots_loaded(SyncRootsResult(roots=[]))

    assert view._checklist_card.isHidden() is False
    assert view._checklist_strip.isHidden() is True
    assert view._synced_roots_group.isHidden() is True
    # One primary action per state (DESIGN.md §4.3): the card's own.
    assert view._add_source_button.isHidden() is True
    card_add = next(b for b in view._checklist_card.findChildren(QPushButton) if b.text() == "Add a source")
    assert card_add.property("buttonKind") == "primary"
    source_item = view._build_checklist_items(
        setup_health.SetupHealth(signed_in=True, has_source=False, claude_desktop="not_connected")
    )[1]
    assert source_item.message == (
        "Point Jarvis at a folder or Google Drive. It reads the files once, then keeps them current."
    )


def test_connectors_view_shows_strip_when_has_sources_but_not_all_done(qtbot, monkeypatch):
    from jarvis.modules.desktop_app.views import connectors_view as connectors_view_mod

    monkeypatch.setattr(connectors_view_mod.cds, "build_plan", lambda: _claude_desktop_plan(cds.CASE_MISSING))

    view = ConnectorsView(QThreadPool())
    qtbot.addWidget(view)

    view._on_synced_roots_loaded(SyncRootsResult(roots=[_sync_root("filesystem", _p("watched"))]))

    assert view._checklist_card.isHidden() is True
    assert view._checklist_strip.isHidden() is False
    assert view._synced_roots_group.isHidden() is False
    assert "2 of 3 done. Connect Claude Desktop so Claude can use your files." in (
        view._checklist_strip._rows[0]._label.text()
    )
    assert view._add_source_button.isHidden() is False
    assert view._count_pill.text() == "1 total"


def test_strip_names_the_sign_in_step_when_it_is_outstanding(qtbot, monkeypatch):
    from jarvis.modules.desktop_app.views import connectors_view as connectors_view_mod

    monkeypatch.setattr(session_store, "load_session", lambda: None)
    monkeypatch.setattr(connectors_view_mod.cds, "build_plan", lambda: _claude_desktop_plan(cds.CASE_MISSING))
    view = ConnectorsView(QThreadPool())
    qtbot.addWidget(view)

    view._on_synced_roots_loaded(SyncRootsResult(roots=[_sync_root("filesystem", _p("watched"))]))

    text = view._checklist_strip._rows[0]._label.text()
    assert "done. Sign in to keep using Jarvis." in text
    assert not text.endswith("Signed in")


def test_header_add_source_stays_while_sources_are_unknown(qtbot, monkeypatch):
    """A failed sources load leaves the card on "Checking…" with no button
    of its own - the header's must stay so a source can still be added."""
    from jarvis.modules.desktop_app.views import connectors_view as connectors_view_mod

    monkeypatch.setattr(connectors_view_mod.cds, "build_plan", lambda: _claude_desktop_plan(cds.CASE_MISSING))
    view = ConnectorsView(QThreadPool())
    qtbot.addWidget(view)

    view._on_synced_roots_loaded(ErrorResponse(error=ErrorDetail(code="daemon_unavailable", message="nope")))
    view.refresh_setup_checklist()

    assert view._checklist_card.isHidden() is False
    assert view._add_source_button.isHidden() is False
    assert not any(b.text() == "Add a source" for b in view._checklist_card.findChildren(QPushButton))


def test_card_add_source_is_disabled_while_a_sync_runs(qtbot, monkeypatch):
    from jarvis.modules.desktop_app.views import connectors_view as connectors_view_mod

    monkeypatch.setattr(connectors_view_mod.cds, "build_plan", lambda: _claude_desktop_plan(cds.CASE_MISSING))
    view = ConnectorsView(QThreadPool())
    qtbot.addWidget(view)
    view._on_synced_roots_loaded(SyncRootsResult(roots=[]))
    card_add = next(b for b in view._checklist_card.findChildren(QPushButton) if b.text() == "Add a source")

    view._set_controls_enabled(False)
    assert card_add.isEnabled() is False

    view._set_controls_enabled(True)
    assert card_add.isEnabled() is True


def test_sources_list_header_is_sentence_case_with_no_ordering_claim(qtbot):
    from PySide6.QtWidgets import QLabel

    view = ConnectorsView(QThreadPool())
    qtbot.addWidget(view)

    texts = [label.text() for label in view._synced_roots_group.findChildren(QLabel)]
    assert "Your sources" in texts
    assert not any("Prioritized" in text for text in texts)


def test_sources_banner_lets_success_be_dismissed_but_keeps_errors(qtbot):
    view = ConnectorsView(QThreadPool())
    qtbot.addWidget(view)

    view._show_sync_failure("unknown", "Something went wrong. Everything already saved is safe.")
    assert view._status_banner._dismiss_button.isHidden()

    view._status_banner.show_success("All caught up.")
    assert not view._status_banner._dismiss_button.isHidden()


def test_connectors_view_hides_checklist_when_all_done(qtbot, monkeypatch):
    from jarvis.modules.desktop_app.views import connectors_view as connectors_view_mod

    frozen = {"command": "x", "args": ["--mcp"]}
    monkeypatch.setattr(connectors_view_mod.cds, "frozen_mcp_entry", lambda: frozen)
    monkeypatch.setattr(
        connectors_view_mod.cds, "build_plan",
        lambda: _claude_desktop_plan(cds.CASE_HAS_MCP_SERVERS, existing_jarvis_entry=frozen),
    )

    view = ConnectorsView(QThreadPool())
    qtbot.addWidget(view)

    view._on_synced_roots_loaded(SyncRootsResult(roots=[_sync_root("filesystem", _p("watched"))]))

    assert view._checklist_card.isHidden() is True
    assert view._checklist_strip.isHidden() is True
    assert view._synced_roots_group.isHidden() is False


def test_claude_desktop_row_message_names_the_real_state(qtbot):
    """DESIGN.md §6 gives Claude Desktop a different sentence per real
    state, not one static label with only the action button changing."""
    view = ConnectorsView(QThreadPool())
    qtbot.addWidget(view)

    def _health(claude_desktop):
        return setup_health.SetupHealth(signed_in=True, has_source=True, claude_desktop=claude_desktop)

    assert view._build_checklist_items(_health("needs_repair"))[2].label == "The Claude connection needs repair."
    assert (
        view._build_checklist_items(_health("translocated"))[2].label
        == "Move Jarvis to your Applications folder and reopen it."
    )
    assert view._build_checklist_items(_health("not_connected"))[2].label == "Connect Claude Desktop (optional)."
    assert view._build_checklist_items(_health("connected"))[2].label == "Connect Claude Desktop (optional)."


def test_not_now_on_source_step_reveals_empty_group_instead_of_card(qtbot, monkeypatch):
    from jarvis.modules.desktop_app.views import connectors_view as connectors_view_mod

    monkeypatch.setattr(connectors_view_mod.cds, "build_plan", lambda: _claude_desktop_plan(cds.CASE_MISSING))

    view = ConnectorsView(QThreadPool())
    qtbot.addWidget(view)
    view._on_synced_roots_loaded(SyncRootsResult(roots=[]))

    view._on_source_not_now_clicked()

    assert view._checklist_card.isHidden() is True
    assert view._synced_roots_group.isHidden() is False


def test_claude_desktop_dismiss_hides_row_and_persists(qtbot, monkeypatch, tmp_path):
    from jarvis.modules.desktop_app.views import connectors_view as connectors_view_mod
    from PySide6.QtCore import QSettings

    ini_path = tmp_path / "dismiss_test.ini"
    monkeypatch.setattr(
        connectors_view_mod.setup_checklist, "_settings",
        lambda: QSettings(str(ini_path), QSettings.Format.IniFormat),
    )
    monkeypatch.setattr(connectors_view_mod.cds, "build_plan", lambda: _claude_desktop_plan(cds.CASE_MISSING))

    view = ConnectorsView(QThreadPool())
    qtbot.addWidget(view)
    view._on_synced_roots_loaded(SyncRootsResult(roots=[_sync_root("filesystem", _p("watched"))]))
    assert view._checklist_strip.isHidden() is False  # Claude Desktop still outstanding

    view._on_dismiss_claude_desktop_clicked()

    assert connectors_view_mod.setup_checklist.is_claude_desktop_dismissed() is True
    assert view._checklist_strip.isHidden() is True  # dismissing completed the checklist
    assert view._synced_roots_group.isHidden() is False


def test_signed_in_quit_action_reopens_the_real_sign_in_dialog_with_first_run_true(qtbot, monkeypatch):
    from jarvis.modules.desktop_app.views import connectors_view as connectors_view_mod

    monkeypatch.setattr(session_store, "load_session", lambda: None)  # the rare escape-hatch case
    monkeypatch.setattr(connectors_view_mod.cds, "build_plan", lambda: _claude_desktop_plan(cds.CASE_MISSING))

    captured = {}

    class _FakeSignInDialog:
        def __init__(self, parent, pool, *, first_run=False):
            captured["first_run"] = first_run

        def exec(self):
            return None

    monkeypatch.setattr(connectors_view_mod, "SignInDialog", _FakeSignInDialog)

    view = ConnectorsView(QThreadPool())
    qtbot.addWidget(view)
    view._on_synced_roots_loaded(SyncRootsResult(roots=[]))

    view._on_signed_in_quit_clicked()

    assert captured["first_run"] is True


def test_ingest_succeeded_refreshes_the_checklist(qtbot, monkeypatch):
    from jarvis.modules.desktop_app.views import connectors_view as connectors_view_mod

    monkeypatch.setattr(connectors_view_mod.cds, "build_plan", lambda: _claude_desktop_plan(cds.CASE_MISSING))
    monkeypatch.setattr(
        connectors_view_mod.daemon_client, "get_sync_roots",
        lambda: SyncRootsResult(roots=[_sync_root("filesystem", _p("watched"))]),
    )

    view = ConnectorsView(QThreadPool())
    qtbot.addWidget(view)
    view._on_synced_roots_loaded(SyncRootsResult(roots=[]))
    assert view._checklist_card.isHidden() is False  # starts at zero sources

    view.ingest_succeeded.emit({})
    qtbot.waitUntil(lambda: view._has_source is True, timeout=2000)

    assert view._checklist_card.isHidden() is True
    assert view._checklist_strip.isHidden() is False


# ---------------------------------------------------------------------
# Sources nav-rail tag signals (DESIGN.md §4.1, Phase 4) - ConnectorsView
# emits these; NavRail's own tests cover rendering the resulting state.
# ---------------------------------------------------------------------

def _empty_ingest_result() -> dict:
    return {
        "sync": {"pulled": 0, "fast_skipped": 0, "skipped": 0, "errors": 0},
        "chunks_created": 0, "embed": {}, "hierarchy": {"skipped": "organize_hierarchy=False"},
    }


def test_start_ingest_emits_sync_state_changed_true(qtbot):
    view = ConnectorsView(QThreadPool())
    qtbot.addWidget(view)
    seen = []
    view.sync_state_changed.connect(seen.append)

    view._start_ingest("x", _empty_ingest_result)

    assert seen == [True]


def test_stop_polling_emits_sync_state_changed_false(qtbot):
    view = ConnectorsView(QThreadPool())
    qtbot.addWidget(view)
    view._start_ingest("x", _empty_ingest_result)
    seen = []
    view.sync_state_changed.connect(seen.append)

    view._stop_polling()

    assert seen == [False]


def test_refresh_setup_checklist_emits_attention_true_for_needs_repair(qtbot, monkeypatch):
    from jarvis.modules.desktop_app.views import connectors_view as connectors_view_mod

    monkeypatch.setattr(
        connectors_view_mod.cds, "build_plan",
        lambda: _claude_desktop_plan(cds.CASE_HAS_MCP_SERVERS, existing_jarvis_entry={"command": "stale", "args": []}),
    )
    monkeypatch.setattr(connectors_view_mod.cds, "frozen_mcp_entry", lambda: {"command": "x", "args": ["--mcp"]})

    view = ConnectorsView(QThreadPool())
    qtbot.addWidget(view)
    seen = []
    view.setup_attention_changed.connect(seen.append)

    view._on_synced_roots_loaded(SyncRootsResult(roots=[_sync_root("filesystem", _p("watched"))]))

    assert seen == [True]


def test_refresh_setup_checklist_emits_attention_false_for_plain_not_yet_done(qtbot, monkeypatch):
    """A fresh install (zero sources, Claude Desktop simply not
    connected yet) is normal onboarding, not a regression - must not
    light up the amber nav tag."""
    from jarvis.modules.desktop_app.views import connectors_view as connectors_view_mod

    monkeypatch.setattr(connectors_view_mod.cds, "build_plan", lambda: _claude_desktop_plan(cds.CASE_MISSING))

    view = ConnectorsView(QThreadPool())
    qtbot.addWidget(view)
    seen = []
    view.setup_attention_changed.connect(seen.append)

    view._on_synced_roots_loaded(SyncRootsResult(roots=[]))

    assert seen == [False]


def test_refresh_setup_checklist_emits_attention_true_when_not_signed_in(qtbot, monkeypatch):
    from jarvis.modules.desktop_app.views import connectors_view as connectors_view_mod

    monkeypatch.setattr(session_store, "load_session", lambda: None)
    monkeypatch.setattr(connectors_view_mod.cds, "build_plan", lambda: _claude_desktop_plan(cds.CASE_MISSING))

    view = ConnectorsView(QThreadPool())
    qtbot.addWidget(view)
    seen = []
    view.setup_attention_changed.connect(seen.append)

    view._on_synced_roots_loaded(SyncRootsResult(roots=[]))

    assert seen == [True]


# ---------------------------------------------------------------------
# Hierarchy reload (confirmed real gap: load_top_level() was only ever
# called once, at app startup - nothing re-triggered it afterward).
# Fixed two ways: a manual Refresh button, and MainWindow reloading it
# automatically after ConnectorsView.ingest_succeeded. Verified with the
# same structural approach already validated for this widget
# (instantiate HierarchyView directly, drive it with real-shaped model
# objects, check topLevelItemCount()/children via QTreeWidget's own
# API) - specifically checking that a *second* load with *different*
# data actually replaces what's there, not just that a first load works.
# ---------------------------------------------------------------------

def _topic(id_, name, direct=0, subtree=0, children=None):
    return TopicNode(id=id_, name=name, path=f"/{name}", level=0, direct_chunk_count=direct,
                      subtree_chunk_count=subtree, children=children)


def test_hierarchy_view_second_load_with_different_data_replaces_the_first(qtbot):
    """The exact failure pattern flagged as worth guarding against: a
    view holding stale internal state that a later load doesn't
    actually clear out. First load: 1 topic. Second load: 4 different
    topics. The tree afterward must reflect only the second load - not
    5 (stale + new), not still 1 (never actually replaced)."""
    banner = DaemonStatusBanner()
    view = HierarchyView(QThreadPool(), banner)
    qtbot.addWidget(view)

    first = HierarchyResult(topics=[_topic("t1", "Coffee")])
    view._on_top_level_loaded(first)
    assert view._tree.topLevelItemCount() == 1
    assert view._tree.topLevelItem(0).text(0) == "Coffee"

    second = HierarchyResult(topics=[
        _topic("t2", "Information Architecture", subtree=85, children=[_topic("t2a", "Sub A")]),
        _topic("t3", "SQLite Connection Management", direct=11, subtree=11),
        _topic("t4", "Technical Implementation", subtree=119, children=[_topic("t4a", "Sub B")]),
        _topic("t5", "Unfiled", direct=46, subtree=46),
    ])
    view._on_top_level_loaded(second)

    assert view._tree.topLevelItemCount() == 4, (
        f"expected exactly the second load's 4 topics, got {view._tree.topLevelItemCount()} - "
        "stale data from the first load was not properly replaced"
    )
    names = {view._tree.topLevelItem(i).text(0) for i in range(4)}
    assert names == {"Information Architecture", "SQLite Connection Management",
                      "Technical Implementation", "Unfiled"}
    assert "Coffee" not in names
    # Children counts, per topic - confirms the second load's nested
    # structure is real, not just top-level labels. "Information
    # Architecture"/"Technical Implementation" were given real
    # `children=[...]`, so their childCount() is that real child.
    # "SQLite Connection Management" was given `children=None` (not yet
    # expanded) with subtree_chunk_count > 0 - _build_item's own,
    # correct, pre-existing behavior adds a single "Loading…" placeholder
    # there so Qt draws it as expandable, not a bug being asserted here.
    by_name = {view._tree.topLevelItem(i).text(0): view._tree.topLevelItem(i) for i in range(4)}
    assert by_name["Information Architecture"].childCount() == 1
    assert by_name["Information Architecture"].child(0).text(0) == "Sub A"
    assert by_name["Technical Implementation"].childCount() == 1
    assert by_name["Technical Implementation"].child(0).text(0) == "Sub B"
    assert by_name["SQLite Connection Management"].childCount() == 1
    assert by_name["SQLite Connection Management"].child(0).text(0) == "Loading…"


def test_hierarchy_view_refresh_button_reloads_via_the_real_daemon_call_path(qtbot, monkeypatch):
    """Not just "clicking it doesn't crash" - confirms the button is
    wired to the real load_top_level() -> run_daemon_call() ->
    daemon_client.get_hierarchy() path, and that a real (mocked) daemon
    response actually repopulates the tree."""
    from jarvis.modules.desktop_app.views import hierarchy_view as hierarchy_view_mod

    banner = DaemonStatusBanner()
    view = HierarchyView(QThreadPool(), banner)
    qtbot.addWidget(view)

    result = HierarchyResult(topics=[_topic("t1", "Coffee"), _topic("t2", "SQLite")])
    monkeypatch.setattr(hierarchy_view_mod.daemon_client, "get_hierarchy", lambda: result)

    view._refresh_button.click()

    qtbot.waitUntil(lambda: view._tree.topLevelItemCount() == 2, timeout=2000)
    names = {view._tree.topLevelItem(i).text(0) for i in range(2)}
    assert names == {"Coffee", "SQLite"}


def test_hierarchy_view_refresh_shows_a_busy_state_while_the_call_is_in_flight(qtbot, monkeypatch):
    """docs/desktop-app-ux-redesign-DESIGN.md §5/§6's Refresh
    busy-button item - a real blocked call, not a fast-returning stub,
    so the disabled/relabeled state is genuinely observable mid-flight,
    not just set-then-immediately-reverted too fast to matter."""
    from jarvis.modules.desktop_app.views import hierarchy_view as hierarchy_view_mod

    release = threading.Event()

    def _blocking_get_hierarchy():
        assert release.wait(timeout=5)
        return HierarchyResult(topics=[])

    monkeypatch.setattr(hierarchy_view_mod.daemon_client, "get_hierarchy", _blocking_get_hierarchy)

    banner = DaemonStatusBanner()
    view = HierarchyView(QThreadPool(), banner)
    qtbot.addWidget(view)

    view._refresh_button.click()

    qtbot.waitUntil(lambda: not view._refresh_button.isEnabled(), timeout=2000)
    assert view._refresh_button.text() == "Refreshing…"

    release.set()

    qtbot.waitUntil(lambda: view._refresh_button.isEnabled(), timeout=2000)
    assert view._refresh_button.text() == "Refresh"


def test_hierarchy_view_refresh_busy_state_reverts_on_an_error_response_too(qtbot):
    """get_hierarchy() never raises (daemon_worker.py's own docstring) -
    a daemon-unreachable failure comes back as an ErrorResponse via
    on_success, not on_error - so _on_top_level_loaded is the only
    place that can revert the busy state, and it must do so on this
    branch too, not just the successful one."""
    banner = DaemonStatusBanner()
    view = HierarchyView(QThreadPool(), banner)
    qtbot.addWidget(view)
    view._set_refreshing(True)

    view._on_top_level_loaded(ErrorResponse(error=ErrorDetail(code="daemon_unavailable", message="nope")))

    assert view._refresh_button.isEnabled() is True
    assert view._refresh_button.text() == "Refresh"


def test_connectors_view_emits_ingest_succeeded_on_success(qtbot):
    view = ConnectorsView(QThreadPool())
    qtbot.addWidget(view)

    result = {
        "sync": {"pulled": 1, "fast_skipped": 0, "skipped": 0, "errors": 0},
        "chunks_created": 2,
        "embed": {},
        "hierarchy": {"skipped": "organize_hierarchy=False"},
    }
    with qtbot.waitSignal(view.ingest_succeeded, timeout=1000) as blocker:
        view._on_ingest_result(result)

    assert blocker.args == [result]


def test_connectors_view_does_not_emit_ingest_succeeded_on_daemon_side_error(qtbot):
    view = ConnectorsView(QThreadPool())
    qtbot.addWidget(view)

    received = []
    view.ingest_succeeded.connect(received.append)

    view._on_ingest_result({"error": {"code": "unknown", "message": "Something went wrong."}})

    assert received == []


# ---------------------------------------------------------------------
# Live ingest feedback (the redesign's confirmed real gap, now fixed) -
# docs/desktop-app-ux-redesign-DESIGN.md §4/§6. Covers the busy-button
# pattern, the status banner reflecting polled phase text, and that a
# stale poll tick can't clobber the final outcome once ingest resolves.
# ---------------------------------------------------------------------

def test_connectors_view_disables_button_and_shows_progress_while_ingest_runs(qtbot):
    """Drives the real _start_ingest -> run_daemon_call path (a genuine
    background QThreadPool call, not a synchronous shortcut) with a
    fake ingest function this test controls the timing of, and confirms
    the busy-button pattern actually engages for the duration: the
    button disables, the progress bar becomes visible, and both revert
    once the (fake) daemon call resolves - which is exactly the window
    that used to show zero visible feedback at all."""
    release = threading.Event()
    result = {
        "sync": {"pulled": 1, "fast_skipped": 0, "skipped": 0, "errors": 0},
        "chunks_created": 1, "embed": {}, "hierarchy": {"skipped": "organize_hierarchy=False"},
    }

    def _fake_ingest(path):
        assert release.wait(timeout=5), "test never released the blocked fake ingest"
        return result

    view = ConnectorsView(QThreadPool())
    qtbot.addWidget(view)
    assert view._add_source_button.isEnabled()
    assert not view._poll_timer.isActive()

    view._start_ingest("some-folder", _fake_ingest, "/some/path")

    qtbot.waitUntil(lambda: not view._add_source_button.isEnabled(), timeout=2000)
    assert view._poll_timer.isActive()

    release.set()

    qtbot.waitUntil(lambda: view._add_source_button.isEnabled(), timeout=2000)
    assert not view._poll_timer.isActive()
    assert "All caught up" in view._status_banner.message()


def test_connectors_view_re_enables_button_and_hides_progress_on_ingest_error(qtbot):
    def _failing_ingest(path):
        raise RuntimeError("boom")

    view = ConnectorsView(QThreadPool())
    qtbot.addWidget(view)

    view._start_ingest("some-folder", _failing_ingest, "/some/path")

    qtbot.waitUntil(lambda: view._add_source_button.isEnabled(), timeout=2000)
    assert not view._poll_timer.isActive()


def _no_modal_dialogs(monkeypatch):
    """DESIGN.md §4.10/§5.4: failures never appear in a modal."""
    from PySide6.QtWidgets import QMessageBox

    for name in ("critical", "warning", "information", "exec"):
        monkeypatch.setattr(
            QMessageBox, name, lambda *a, **k: pytest.fail("a failure must not open a modal dialog"),
        )


def _banner_state(view):
    banner = view._status_banner
    return {
        "text": banner.message(),
        "kind": banner.property("bannerKind"),
        "action": banner._action_button.text() if not banner._action_button.isHidden() else None,
        "secondary": banner._secondary_button.text() if not banner._secondary_button.isHidden() else None,
    }


def test_an_unexpected_sync_failure_shows_a_calm_banner_with_retry_and_feedback(qtbot, monkeypatch):
    _no_modal_dialogs(monkeypatch)
    view = ConnectorsView(QThreadPool())
    qtbot.addWidget(view)

    view._on_ingest_error(RuntimeError("daemon exploded at C:/Users/someone/file.md"))

    assert _banner_state(view) == {
        "text": "Something went wrong. Everything already saved is safe.",
        "kind": "error",
        "action": "Retry",
        "secondary": "Send feedback",
    }


@pytest.mark.parametrize("code, action", [
    ("embedding_key_invalid", "Fix API key"),
    ("drive_auth_expired", "Reconnect Drive"),
    ("network_unavailable", "Retry"),
    ("rate_limited", "Retry"),
    ("organize_failed", "Retry"),
])
def test_a_failed_sync_offers_the_action_that_fixes_it(qtbot, monkeypatch, code, action):
    """One action per failure, the one that fixes it (DESIGN.md §6); the
    message is the background service's own calm text, shown as-is."""
    _no_modal_dialogs(monkeypatch)
    view = ConnectorsView(QThreadPool())
    qtbot.addWidget(view)

    view._on_ingest_result({"error": {"code": code, "message": "Paused. Calm text from the service."}})

    assert _banner_state(view) == {
        "text": "Paused. Calm text from the service.", "kind": "error", "action": action, "secondary": None,
    }


def test_an_unreachable_background_service_shows_its_own_banner(qtbot, monkeypatch):
    from jarvis.modules.daemon.client import DaemonConnectionError

    _no_modal_dialogs(monkeypatch)
    view = ConnectorsView(QThreadPool())
    qtbot.addWidget(view)

    view._on_ingest_error(DaemonConnectionError("isn't responding"))

    state = _banner_state(view)
    assert state["text"] == "The background service isn't running. Everything already saved is safe."
    assert state["action"] == "Retry"


def test_fix_api_key_saves_restarts_the_service_and_retries(qtbot, monkeypatch):
    """Ticket 05: one guided action saves the key, restarts the service
    and retries - a new key only reaches the service when it restarts."""
    from jarvis.modules.desktop_app.views import connectors_view as connectors_view_mod

    monkeypatch.setattr(
        connectors_view_mod.SettingsDialog, "exec", lambda self: connectors_view_mod.QDialog.DialogCode.Accepted,
    )
    restarted = []
    monkeypatch.setattr(connectors_view_mod.process_stats, "force_restart_daemon", lambda: restarted.append(True))
    retried = []
    view = ConnectorsView(QThreadPool())
    qtbot.addWidget(view)
    monkeypatch.setattr(view, "_retry_last_ingest", lambda: retried.append(True))

    view._fix_api_key_and_retry()

    qtbot.waitUntil(lambda: retried == [True], timeout=2000)
    assert restarted == [True]


def test_fix_api_key_cancelled_changes_nothing(qtbot, monkeypatch):
    from jarvis.modules.desktop_app.views import connectors_view as connectors_view_mod

    monkeypatch.setattr(
        connectors_view_mod.SettingsDialog, "exec", lambda self: connectors_view_mod.QDialog.DialogCode.Rejected,
    )
    monkeypatch.setattr(
        connectors_view_mod.process_stats, "force_restart_daemon",
        lambda: pytest.fail("must not restart the service when the key wasn't changed"),
    )
    view = ConnectorsView(QThreadPool())
    qtbot.addWidget(view)

    view._fix_api_key_and_retry()


def test_reconnect_drive_signs_in_again_then_retries(qtbot, monkeypatch):
    from jarvis.modules.desktop_app.views import connectors_view as connectors_view_mod

    signed_in = []
    monkeypatch.setattr(
        connectors_view_mod.drive, "get_drive_service",
        lambda allow_interactive_auth=None: signed_in.append(allow_interactive_auth) or object(),
    )
    retried = []
    view = ConnectorsView(QThreadPool())
    qtbot.addWidget(view)
    monkeypatch.setattr(view, "_retry_last_ingest", lambda: retried.append(True))

    view._reconnect_drive_and_retry()

    qtbot.waitUntil(lambda: retried == [True], timeout=2000)
    assert signed_in == [True]


def test_connectors_view_status_line_reflects_the_polled_phase(qtbot):
    view = ConnectorsView(QThreadPool())
    qtbot.addWidget(view)
    view._poll_timer.start()  # _on_status_polled bails out unless polling is active

    view._on_status_polled({"phase": "Reading files", "in_progress": True})
    assert "Reading files" in view._last_progress_text

    view._on_status_polled({"phase": "Generating embeddings", "in_progress": True})
    assert view._last_progress_text == "Making files searchable…"

    view._poll_timer.stop()


def test_connectors_view_embed_progress_fills_the_bar_without_internal_counts(qtbot):
    view = ConnectorsView(QThreadPool())
    qtbot.addWidget(view)
    view._poll_timer.start()

    view._on_status_polled({
        "phase": "Generating embeddings",
        "batch_progress": {
            "current_batch": 2, "total_batches": 5, "processed_items": 40, "total_items": 100,
        },
        "progress": {"processed": 40, "total": 100, "errors": 0},
        "in_progress": True,
    })

    assert view._last_progress_text == "Making files searchable…"
    assert view._last_progress_fraction == 0.4

    view._poll_timer.stop()


def test_connectors_view_status_line_shows_bare_phase_without_batch_progress(qtbot):
    """No batch_progress yet (still in an earlier phase, or a pure
    cache-hit run that never embeds anything) - the status line should
    show the bare phase name, not a stale or zeroed-out count."""
    view = ConnectorsView(QThreadPool())
    qtbot.addWidget(view)
    view._poll_timer.start()

    view._on_status_polled({"phase": "Reading files", "batch_progress": None, "in_progress": True})

    assert view._last_progress_text == "Reading files…"

    view._poll_timer.stop()


def test_connectors_view_status_line_reflects_a_cancel_request(qtbot):
    view = ConnectorsView(QThreadPool())
    qtbot.addWidget(view)
    view._poll_timer.start()

    view._on_status_polled({"phase": "Generating embeddings", "cancel_requested": True, "in_progress": True})

    assert view._cancel_requested is True

    view._poll_timer.stop()


def test_connectors_view_ignores_a_stale_status_poll_after_ingest_already_resolved(qtbot):
    """Real race this guards against: a poll tick already in flight the
    moment the main ingest call resolves would otherwise land afterward
    and briefly overwrite "Sync complete" with a stale phase."""
    view = ConnectorsView(QThreadPool())
    qtbot.addWidget(view)

    view._on_ingest_result({
        "sync": {"pulled": 1, "fast_skipped": 0, "skipped": 0, "errors": 0},
        "chunks_created": 1, "embed": {}, "hierarchy": {"skipped": "organize_hierarchy=False"},
    })
    assert "All caught up" in view._status_banner.message()

    view._on_status_polled({"phase": "Generating embeddings", "in_progress": True})

    assert "All caught up" in view._status_banner.message()


# ---------------------------------------------------------------------
# Cancel (Ingest progress backend: full-phase tracking, cancel, run
# history schema, #118) - cooperative, not instant: clicking Cancel
# fires the daemon call and updates the status line right away, but the
# ingest itself only actually stops once its own eventual response
# says so (_on_ingest_result's "cancelled" branch below).
# ---------------------------------------------------------------------

# A minimal but real-shaped ingest result - _on_ingest_result indexes
# into result["sync"]["errors"] unconditionally, so a fake ingest_fn
# returning bare {} is a landmine (a real run_ingest_filesystem/
# run_ingest_drive success always has this shape) that only fails to
# detonate depending on exactly when the queued success signal is
# delivered relative to the test's own teardown.
_STUB_INGEST_RESULT = {
    "sync": {"pulled": 0, "fast_skipped": 0, "skipped": 0, "errors": 0},
    "chunks_created": 0, "embed": {}, "hierarchy": {},
}


def test_connectors_view_cancel_button_calls_the_real_cancel_endpoint(qtbot, monkeypatch):
    from jarvis.modules.desktop_app.views import connectors_view as connectors_view_mod

    captured = []
    monkeypatch.setattr(
        connectors_view_mod.daemon_client, "cancel_ingest",
        lambda: captured.append(True) or {"cancel_requested": True},
    )

    release = threading.Event()
    view = ConnectorsView(QThreadPool())
    qtbot.addWidget(view)
    view._start_ingest(
        "some-folder", lambda path: release.wait(timeout=5) and _STUB_INGEST_RESULT, "/some/path"
    )

    view._on_cancel_clicked()

    qtbot.waitUntil(lambda: captured == [True], timeout=2000)
    assert view._cancel_requested is True
    release.set()


def test_connectors_view_cancel_button_disables_immediately_to_prevent_double_clicks(qtbot, monkeypatch):
    from jarvis.modules.desktop_app.views import connectors_view as connectors_view_mod

    release = threading.Event()

    def _blocking_cancel():
        assert release.wait(timeout=5), "test never released the blocked cancel call"
        return {"cancel_requested": True}

    monkeypatch.setattr(connectors_view_mod.daemon_client, "cancel_ingest", _blocking_cancel)

    view = ConnectorsView(QThreadPool())
    qtbot.addWidget(view)
    view._start_ingest("some-folder", lambda path: _STUB_INGEST_RESULT, "/some/path")

    view._on_cancel_clicked()

    assert view._cancel_requested is True
    release.set()


def test_connectors_view_a_failed_cancel_request_shows_an_error_and_re_enables_the_button(qtbot, monkeypatch):
    from jarvis.modules.desktop_app.views import connectors_view as connectors_view_mod

    _no_modal_dialogs(monkeypatch)

    def _boom():
        raise RuntimeError("daemon unreachable")

    monkeypatch.setattr(connectors_view_mod.daemon_client, "cancel_ingest", _boom)

    release = threading.Event()
    view = ConnectorsView(QThreadPool())
    qtbot.addWidget(view)
    view._start_ingest(
        "some-folder", lambda path: release.wait(timeout=5) and _STUB_INGEST_RESULT, "/some/path"
    )

    view._on_cancel_clicked()

    qtbot.waitUntil(lambda: not view._cancel_requested, timeout=2000)
    assert _banner_state(view)["text"] == "The sync is still running. Jarvis couldn't stop it."
    release.set()


def test_connectors_view_on_ingest_result_handles_a_cancelled_run(qtbot):
    """The daemon returns {"cancelled": true, ...} (200, not the error
    envelope) when a cooperative cancel actually lands - a materially
    different outcome from both success and a real failure, so it gets
    its own status-banner wording rather than reusing either."""
    view = ConnectorsView(QThreadPool())
    qtbot.addWidget(view)
    # Never-released wait, not a real fake ingest_fn - this test drives
    # _on_ingest_result directly, so the real backgrounded call must
    # never itself resolve (and hit the success path) during the test.
    view._start_ingest("some-folder", lambda path: threading.Event().wait(timeout=5), "/some/path")

    view._on_ingest_result({"cancelled": True, "partial": {"sync": {"pulled": 1}}})

    assert "Stopped" in view._status_banner.message()
    assert "1 file" in view._status_banner.message()
    assert view._status_banner._action_button.text() == "Retry"
    assert not view._poll_timer.isActive()
    assert view._add_source_button.isEnabled()


def test_retry_replays_the_same_ingest_call_and_reads_as_retrying(qtbot):
    calls = []

    def _ingest_fn(path):
        calls.append(path)
        return _STUB_INGEST_RESULT

    view = ConnectorsView(QThreadPool())
    qtbot.addWidget(view)
    view._start_ingest("some-folder", _ingest_fn, "/some/path")
    qtbot.waitUntil(lambda: calls == ["/some/path"], timeout=2000)
    qtbot.waitUntil(lambda: view._add_source_button.isEnabled(), timeout=2000)

    view._retry_last_ingest()

    assert "Retrying" in view._status_banner.message()
    qtbot.waitUntil(lambda: calls == ["/some/path", "/some/path"], timeout=2000)


def test_connectors_view_polls_the_real_daemon_client_get_ingest_status(qtbot, monkeypatch):
    """Not just "_on_status_polled works in isolation" (the tests above)
    - confirms _poll_ingest_status is wired to the real run_daemon_call
    -> daemon_client.get_ingest_status path, the same seam every other
    daemon call in this app already goes through."""
    from jarvis.modules.desktop_app.views import connectors_view as connectors_view_mod

    monkeypatch.setattr(
        connectors_view_mod.daemon_client, "get_ingest_status",
        lambda: {"phase": "Organizing into topics", "in_progress": True},
    )

    view = ConnectorsView(QThreadPool())
    qtbot.addWidget(view)
    view._poll_timer.start()

    view._poll_ingest_status()

    qtbot.waitUntil(lambda: view._last_progress_text == "Organizing topics…", timeout=2000)

    view._poll_timer.stop()


def test_connectors_view_survives_a_failed_status_poll_without_disturbing_ui_state(qtbot, monkeypatch):
    """Investigated after a real manual-testing report of GET
    /ingest/status occasionally returning an empty body (daemon_client.
    call() already turns that into a raised DaemonConnectionError, never
    a raw unhandled exception - see connectors_view.py's module
    docstring for the full investigation). This confirms the client
    side of that: a failed poll (whatever the cause) must not crash the
    view, must not touch the status banner or progress bar, and must
    not stop the button from re-enabling once the real ingest call
    resolves - a poll failure is background noise, not something that
    can affect the visible ingest lifecycle."""
    from jarvis.modules.daemon.client import DaemonConnectionError
    from jarvis.modules.desktop_app.views import connectors_view as connectors_view_mod

    def _boom():
        raise DaemonConnectionError("The Jarvis background service isn't responding.")

    monkeypatch.setattr(connectors_view_mod.daemon_client, "get_ingest_status", _boom)

    view = ConnectorsView(QThreadPool())
    qtbot.addWidget(view)
    view._start_ingest("some-folder", lambda path: {
        "sync": {"pulled": 1, "fast_skipped": 0, "skipped": 0, "errors": 0},
        "chunks_created": 1, "embed": {}, "hierarchy": {"skipped": "organize_hierarchy=False"},
    }, "/some/path")

    view._poll_ingest_status()  # the real run_daemon_call -> _boom -> on_error path

    qtbot.waitUntil(lambda: view._add_source_button.isEnabled(), timeout=2000)
    assert "All caught up" in view._status_banner.message()


# ---------------------------------------------------------------------
# Add-source chooser (DESIGN.md §4.10, Phase 7) - AddSourceDialog's own
# rendering is covered by test_desktop_app_add_source_dialog.py; these
# confirm ConnectorsView actually opens it and dispatches correctly.
# ---------------------------------------------------------------------

def test_add_source_button_opens_the_chooser_and_dispatches_to_folder(qtbot, monkeypatch):
    from jarvis.modules.desktop_app.views import connectors_view as connectors_view_mod

    called = []
    monkeypatch.setattr(connectors_view_mod.ConnectorsView, "_on_add_folder", lambda self: called.append("folder"))
    monkeypatch.setattr(connectors_view_mod.ConnectorsView, "_on_connect_drive", lambda self: called.append("drive"))
    monkeypatch.setattr(
        connectors_view_mod, "AddSourceDialog",
        lambda parent=None: _FakeAddSourceDialog(accept=True, choice=connectors_view_mod.add_source_dialog.FOLDER),
    )

    view = ConnectorsView(QThreadPool())
    qtbot.addWidget(view)

    view._add_source_button.click()

    assert called == ["folder"]


def test_add_source_button_opens_the_chooser_and_dispatches_to_drive(qtbot, monkeypatch):
    from jarvis.modules.desktop_app.views import connectors_view as connectors_view_mod

    called = []
    monkeypatch.setattr(connectors_view_mod.ConnectorsView, "_on_add_folder", lambda self: called.append("folder"))
    monkeypatch.setattr(connectors_view_mod.ConnectorsView, "_on_connect_drive", lambda self: called.append("drive"))
    monkeypatch.setattr(
        connectors_view_mod, "AddSourceDialog",
        lambda parent=None: _FakeAddSourceDialog(accept=True, choice=connectors_view_mod.add_source_dialog.DRIVE),
    )

    view = ConnectorsView(QThreadPool())
    qtbot.addWidget(view)

    view._add_source_button.click()

    assert called == ["drive"]


def test_cancelling_the_add_source_chooser_dispatches_nothing(qtbot, monkeypatch):
    from jarvis.modules.desktop_app.views import connectors_view as connectors_view_mod

    called = []
    monkeypatch.setattr(connectors_view_mod.ConnectorsView, "_on_add_folder", lambda self: called.append("folder"))
    monkeypatch.setattr(connectors_view_mod.ConnectorsView, "_on_connect_drive", lambda self: called.append("drive"))
    monkeypatch.setattr(
        connectors_view_mod, "AddSourceDialog",
        lambda parent=None: _FakeAddSourceDialog(accept=False),
    )

    view = ConnectorsView(QThreadPool())
    qtbot.addWidget(view)

    view._add_source_button.click()

    assert called == []


def test_checklist_add_a_source_action_opens_the_chooser_too(qtbot, monkeypatch):
    """Not a direct folder pick anymore (Phase 7) - otherwise Drive would
    be unreachable from onboarding once the main buttons row no longer
    offers it directly."""
    from jarvis.modules.desktop_app.views import connectors_view as connectors_view_mod

    monkeypatch.setattr(connectors_view_mod.cds, "build_plan", lambda: _claude_desktop_plan(cds.CASE_MISSING))
    called = []
    monkeypatch.setattr(connectors_view_mod.ConnectorsView, "_on_add_folder", lambda self: called.append("folder"))
    monkeypatch.setattr(
        connectors_view_mod, "AddSourceDialog",
        lambda parent=None: _FakeAddSourceDialog(accept=True, choice=connectors_view_mod.add_source_dialog.FOLDER),
    )

    view = ConnectorsView(QThreadPool())
    qtbot.addWidget(view)
    view._on_synced_roots_loaded(SyncRootsResult(roots=[]))

    add_source_action = next(b for b in view._checklist_card.findChildren(QPushButton) if b.text() == "Add a source")
    add_source_action.click()

    assert called == ["folder"]


class _FakeAddSourceDialog:
    """Stands in for the real AddSourceDialog - exec()/chosen() are the
    only two methods ConnectorsView calls on it."""

    def __init__(self, accept: bool, choice=None):
        self._accept = accept
        self._choice = choice

    def exec(self):
        from PySide6.QtWidgets import QDialog
        return QDialog.DialogCode.Accepted if self._accept else QDialog.DialogCode.Rejected

    def chosen(self):
        return self._choice


# ---------------------------------------------------------------------
# Google Drive connector (docs/desktop-app-drive-connector-DESIGN.md) -
# "Connect Google Drive…" -> get_drive_service() threaded off the GUI
# thread through the same run_daemon_call seam as everything else in
# this view, then the confirmed folder handed to the exact same
# _start_ingest path filesystem ingest already uses.
# ---------------------------------------------------------------------

def test_connectors_view_add_source_button_starts_enabled(qtbot):
    """Confirmed real gap this closes (pre-Phase-7, when this was still
    two separate buttons): one used to be permanently disabled with a
    "not implemented yet" tooltip."""
    view = ConnectorsView(QThreadPool())
    qtbot.addWidget(view)

    assert view._add_source_button.isEnabled()


def test_connectors_view_connect_drive_runs_get_drive_service_off_the_gui_thread(qtbot, monkeypatch):
    """Proves the OAuth call is genuinely threaded, not just wired to a
    function that happens to return fast: blocks the fake
    get_drive_service on an Event only the test controls, and confirms
    the GUI (both buttons disabled, progress bar visible) stays
    responsive - qtbot.waitUntil below only succeeds if the GUI event
    loop is still pumping - while that background call is stuck."""
    from jarvis.modules.desktop_app.views import connectors_view as connectors_view_mod

    release = threading.Event()

    def _blocking_get_drive_service(allow_interactive_auth=None):
        assert allow_interactive_auth is True
        assert release.wait(timeout=5), "test never released the blocked call"
        return object()

    monkeypatch.setattr(connectors_view_mod.drive, "get_drive_service", _blocking_get_drive_service)
    monkeypatch.setattr(
        connectors_view_mod, "DriveFolderDialog",
        lambda *a, **k: _FakeDriveDialog(accept=False),
    )

    view = ConnectorsView(QThreadPool())
    qtbot.addWidget(view)

    view._on_connect_drive()

    qtbot.waitUntil(lambda: not view._add_source_button.isEnabled(), timeout=2000)
    # No early-cancel affordance for the OAuth wait itself (module
    # docstring) - the stepper/cancel button only ever appear once a
    # real ingest (the confirmed folder) actually starts.
    assert view._status_banner.message() == "Sign in to Google to continue."

    release.set()

    qtbot.waitUntil(lambda: view._add_source_button.isEnabled(), timeout=2000)


class _FakeDriveDialog:
    """Stands in for the real DriveFolderDialog - exec()/selected_folder()
    are the only two methods ConnectorsView calls on it."""

    def __init__(self, accept: bool, folder=("folder-abc", "Reports")):
        self._accept = accept
        self._folder = folder

    def exec(self):
        from PySide6.QtWidgets import QDialog
        return QDialog.DialogCode.Accepted if self._accept else QDialog.DialogCode.Rejected

    def selected_folder(self):
        return self._folder


def test_connectors_view_confirmed_drive_folder_starts_ingest_via_start_ingest(qtbot, monkeypatch):
    """The actual handoff (design doc §3b): a confirmed folder selection
    must go through _start_ingest -> daemon_client.run_ingest_drive,
    the identical seam filesystem ingest uses - not a second,
    parallel implementation of the busy-button/progress/summary
    behavior. Runs the real run_daemon_call path for both
    get_drive_service and run_ingest_drive."""
    from jarvis.modules.desktop_app.views import connectors_view as connectors_view_mod

    fake_service = object()
    monkeypatch.setattr(
        connectors_view_mod.drive, "get_drive_service",
        lambda allow_interactive_auth=None: fake_service,
    )
    monkeypatch.setattr(
        connectors_view_mod, "DriveFolderDialog",
        lambda service, pool, parent: _FakeDriveDialog(accept=True) if service is fake_service else None,
    )

    captured = {}

    def _fake_run_ingest_drive(folder_id, organize_hierarchy=True):
        captured["folder_id"] = folder_id
        return {
            "sync": {"pulled": 3, "fast_skipped": 0, "skipped": 0, "errors": 0},
            "chunks_created": 3, "embed": {}, "hierarchy": {"skipped": "organize_hierarchy=False"},
        }

    monkeypatch.setattr(connectors_view_mod.daemon_client, "run_ingest_drive", _fake_run_ingest_drive)

    view = ConnectorsView(QThreadPool())
    qtbot.addWidget(view)

    view._on_connect_drive()

    qtbot.waitUntil(lambda: "All caught up" in view._status_banner.message(), timeout=2000)
    assert captured["folder_id"] == "folder-abc"
    assert "All caught up" in view._status_banner.message()
    assert view._add_source_button.isEnabled() is True


def test_connectors_view_cancelling_the_drive_dialog_does_not_start_an_ingest(qtbot, monkeypatch):
    from jarvis.modules.desktop_app.views import connectors_view as connectors_view_mod

    monkeypatch.setattr(
        connectors_view_mod.drive, "get_drive_service",
        lambda allow_interactive_auth=None: object(),
    )
    monkeypatch.setattr(
        connectors_view_mod, "DriveFolderDialog",
        lambda *a, **k: _FakeDriveDialog(accept=False),
    )
    ingest_calls = []
    monkeypatch.setattr(
        connectors_view_mod.daemon_client, "run_ingest_drive",
        lambda *a, **k: ingest_calls.append(a) or {},
    )

    view = ConnectorsView(QThreadPool())
    qtbot.addWidget(view)

    view._on_connect_drive()

    qtbot.waitUntil(lambda: view._add_source_button.isEnabled(), timeout=2000)
    assert ingest_calls == []


def test_connectors_view_shows_a_calm_banner_when_drive_connect_fails(qtbot, monkeypatch):
    """A connect failure happens before any sync is requested, so it
    gets its own wording - and never the raw exception text."""
    from jarvis.modules.desktop_app.views import connectors_view as connectors_view_mod

    _no_modal_dialogs(monkeypatch)

    def _boom(allow_interactive_auth=None):
        raise RuntimeError("GOOGLE_OAUTH_CLIENT_ID / GOOGLE_OAUTH_CLIENT_SECRET are not set.")

    monkeypatch.setattr(connectors_view_mod.drive, "get_drive_service", _boom)
    monkeypatch.setattr(connectors_view_mod.error_reporting, "report_error", lambda *a, **k: None)

    view = ConnectorsView(QThreadPool())
    qtbot.addWidget(view)

    view._on_connect_drive()

    qtbot.waitUntil(lambda: view._add_source_button.isEnabled(), timeout=2000)
    state = _banner_state(view)
    assert state["text"] == "Google Drive isn't connected. Nothing was changed."
    assert state["action"] == "Try again"
    assert "GOOGLE_OAUTH" not in state["text"]


def test_connectors_view_auto_reports_a_drive_connect_failure(qtbot, monkeypatch):
    """Regression test: a Drive connect failure (the motivating case: an
    unapproved tester's access_denied) must reach the team automatically,
    alongside the banner the user already sees, not instead of it."""
    from jarvis.modules.desktop_app.views import connectors_view as connectors_view_mod

    def _boom(allow_interactive_auth=None):
        raise RuntimeError("access_denied: not a test user")

    monkeypatch.setattr(connectors_view_mod.drive, "get_drive_service", _boom)

    reported = {}
    monkeypatch.setattr(
        connectors_view_mod.error_reporting, "report_error",
        lambda pool, error_type, message: reported.update(error_type=error_type, message=message),
    )

    view = ConnectorsView(QThreadPool())
    qtbot.addWidget(view)

    view._on_connect_drive()

    qtbot.waitUntil(lambda: "error_type" in reported, timeout=2000)
    assert reported["error_type"] == "drive_connect_failed"
    assert "access_denied" in reported["message"]


def test_main_window_reloads_hierarchy_after_a_successful_ingest(qtbot, monkeypatch):
    """End-to-end wiring test, through the real signal connection
    MainWindow makes - not a unit test of either view in isolation.
    Simulates: app already running with an empty hierarchy (as it
    genuinely was at startup, before any content existed), then an
    ingest completes successfully, and the hierarchy tab must reload
    on its own - reproducing the exact real-world sequence that
    exposed this gap, without needing a real daemon or a real ingest."""
    from jarvis.modules.desktop_app import main_window as main_window_mod
    from jarvis.modules.desktop_app.views import hierarchy_view as hierarchy_view_mod

    monkeypatch.setattr(main_window_mod, "ensure_daemon_running", lambda: 54321)
    monkeypatch.setattr(main_window_mod, "register_client", lambda: "fake-client-id")

    call_count = {"n": 0}
    empty_result = HierarchyResult(topics=[])
    populated_result = HierarchyResult(topics=[_topic("t1", "Coffee"), _topic("t2", "SQLite")])

    def fake_get_hierarchy():
        call_count["n"] += 1
        return empty_result if call_count["n"] == 1 else populated_result

    monkeypatch.setattr(hierarchy_view_mod.daemon_client, "get_hierarchy", fake_get_hierarchy)

    window = MainWindow(QThreadPool())
    qtbot.addWidget(window)

    # Startup's own automatic load - matches what genuinely happened:
    # hierarchy loaded once, correctly empty, before anything was ingested.
    qtbot.waitUntil(lambda: call_count["n"] == 1, timeout=2000)
    assert window.hierarchy_view._tree.topLevelItemCount() == 0

    # A successful ingest, simulated directly on the connectors view -
    # the real signal path (ingest_succeeded -> load_top_level) is what's
    # under test here, not run_ingest_filesystem's own daemon call.
    window.connectors_view._on_ingest_result({
        "sync": {"pulled": 1, "fast_skipped": 0, "skipped": 0, "errors": 0},
        "chunks_created": 2,
        "embed": {},
        "hierarchy": {"skipped": "organize_hierarchy=False"},
    })

    qtbot.waitUntil(lambda: window.hierarchy_view._tree.topLevelItemCount() == 2, timeout=2000)
    names = {window.hierarchy_view._tree.topLevelItem(i).text(0) for i in range(2)}
    assert names == {"Coffee", "SQLite"}


def test_format_phase_progress_counts_files_while_reading_them():
    from jarvis.modules.desktop_app.views.connectors_view import format_phase_progress

    assert format_phase_progress(
        pipeline_mod.PHASE_READING_FILES, {"processed": 310, "total": 500, "errors": 0},
    ) == "Reading files, 310 of 500"
    assert format_phase_progress(
        pipeline_mod.PHASE_READING_FILES, {"processed": 7, "total": None, "errors": 0},
    ) == "Reading files, 7 files so far"


def test_format_phase_progress_never_shows_internal_step_names_or_counts():
    from jarvis.modules.desktop_app.views.connectors_view import format_phase_progress

    texts = [
        format_phase_progress(pipeline_mod.PHASE_PROCESSING_DOCUMENTS, {"processed": 3, "total": 9, "errors": 0}),
        format_phase_progress(pipeline_mod.PHASE_GENERATING_EMBEDDINGS, {"processed": 40, "total": 100, "errors": 0}),
        format_phase_progress(pipeline_mod.PHASE_ORGANIZING, {"processed": 5, "total": 5, "errors": 0}),
        format_phase_progress(pipeline_mod.PHASE_ORGANIZING, None),
        format_phase_progress(pipeline_mod.PHASE_RECONCILING, None),
    ]

    assert texts == [
        "Preparing files…", "Making files searchable…", "Organizing topics…", "Organizing topics…", "Finishing up…",
    ]


def test_connectors_view_banner_names_the_folder_being_ingested(qtbot):
    release = threading.Event()
    view = ConnectorsView(QThreadPool())
    qtbot.addWidget(view)

    result = {
        "sync": {"pulled": 1, "fast_skipped": 0, "skipped": 0, "errors": 0},
        "chunks_created": 1, "embed": {}, "hierarchy": {"skipped": "organize_hierarchy=False"},
    }
    view._start_ingest("TWS", lambda path: release.wait(timeout=5) and result, "/x/TWS")

    assert "TWS" in view._status_banner.message()
    release.set()
    qtbot.waitUntil(lambda: view._add_source_button.isEnabled(), timeout=2000)
    assert view._status_banner.message() == "All caught up. 1 file saved from “TWS”. Nothing needs your attention."


def test_connectors_view_flags_unreadable_files_as_soon_as_sync_reports_them(qtbot):
    view = ConnectorsView(QThreadPool())
    qtbot.addWidget(view)
    view._source_name = "TWS"
    view._poll_timer.start()

    view._on_status_polled({
        "phase": pipeline_mod.PHASE_READING_FILES,
        "progress": {"processed": 1, "total": 1, "errors": 1},
        "in_progress": True,
    })
    view._on_status_polled({"phase": pipeline_mod.PHASE_ORGANIZING, "progress": None, "in_progress": True})

    assert "1 file couldn't be read" in view._status_banner.message()
    view._poll_timer.stop()


def test_connectors_view_final_banner_warns_when_sync_had_errors(qtbot):
    view = ConnectorsView(QThreadPool())
    qtbot.addWidget(view)
    view._source_name = "TWS"

    view._on_ingest_result({
        "sync": {"pulled": 2, "fast_skipped": 0, "skipped": 0, "errors": 1},
        "chunks_created": 0, "embed": {}, "hierarchy": {"skipped": "organize_hierarchy=False"},
    })

    text = view._status_banner.message()
    assert text == "Mostly done. 1 file couldn't be read. The other 2 files are saved."
    assert view._status_banner._action_button.text() == "Retry"


# ---------------------------------------------------------------------
# format_utils - pure functions, no Qt involved.
# ---------------------------------------------------------------------

def test_format_bytes_picks_the_right_unit():
    assert format_utils.format_bytes(0) == "0.0 B"
    assert format_utils.format_bytes(512) == "512.0 B"
    assert format_utils.format_bytes(1024) == "1.0 KB"
    assert format_utils.format_bytes(int(155.0 * 1024 * 1024)) == "155.0 MB"


def test_format_duration_picks_the_right_units():
    assert format_utils.format_duration(45) == "45s"
    assert format_utils.format_duration(125) == "2m 5s"
    assert format_utils.format_duration(3725) == "1h 2m"


# ---------------------------------------------------------------------
# SettingsView (plan §8) - API/embedding form (shared with SettingsDialog
# via settings_dialog.build_embedding_fields), Integrations, daemon status
# card + Restart/Force Kill (both behind confirm_action), RAM/storage
# display, updates, and log reveal. Every daemon-touching call is mocked
# at the process_stats/daemon_client.client level rather than requiring a
# real daemon, matching this file's existing convention.
# ---------------------------------------------------------------------

def test_settings_view_instantiates(qtbot):
    banner = DaemonStatusBanner()
    view = SettingsView(QThreadPool(), banner, lambda: None)
    qtbot.addWidget(view)

    assert view._daemon_status_label.text() == "Checking…"
    assert view._force_kill_button.property("buttonKind") == "destructive"


def test_settings_view_buttons_are_sentence_case_with_no_ellipses(qtbot, monkeypatch):
    monkeypatch.setattr(session_store, "load_session", lambda: None)
    view = SettingsView(QThreadPool(), DaemonStatusBanner(), lambda: None)
    qtbot.addWidget(view)

    texts = {button.text() for button in view.findChildren(QPushButton)}
    assert {"Send feedback", "Force kill", "Sign in"} <= texts
    assert not any(text.endswith("…") for text in texts)


def test_settings_view_buttons_keep_their_natural_width(qtbot):
    from PySide6.QtCore import Qt

    view = SettingsView(QThreadPool(), DaemonStatusBanner(), lambda: None)
    qtbot.addWidget(view)

    for button in (view._account_button, view._check_updates_button):
        layout = button.parentWidget().layout()
        item = layout.itemAt(layout.indexOf(button))
        assert item.alignment() == Qt.AlignmentFlag.AlignLeft


def test_settings_view_restart_failure_shows_the_catalogue_message_not_the_exception(qtbot):
    from jarvis.modules.daemon.client import DaemonStartupError

    banner = DaemonStatusBanner()
    view = SettingsView(QThreadPool(), banner, lambda: None)
    qtbot.addWidget(view)

    view._on_daemon_action_failed(DaemonStartupError("port 54321 refused: WinError 10061"), retry=view._restart)
    assert banner.message() == "The background service isn't running. Everything already saved is safe."
    assert banner._action_button.text() == "Restart service"
    assert banner._dismiss_button.isHidden()  # stays until resolved (DESIGN.md §4.2)

    view._on_daemon_action_failed(RuntimeError("Traceback (most recent call last)"), retry=view._restart)
    assert banner.message() == "Something went wrong. Everything already saved is safe."
    assert banner._secondary_button.text() == "Send feedback"


def test_settings_view_restart_service_on_the_banner_force_restarts_without_asking(qtbot, monkeypatch):
    from jarvis.modules.daemon.client import DaemonStartupError
    from jarvis.modules.desktop_app.views import settings_view as settings_view_mod

    monkeypatch.setattr(
        settings_view_mod, "confirm_action", lambda *a, **k: pytest.fail("the banner's restart must not confirm"),
    )
    restarts = []
    monkeypatch.setattr(settings_view_mod.process_stats, "force_restart_daemon", lambda: restarts.append(True))
    monkeypatch.setattr(settings_view_mod, "_restart_and_respawn", lambda _id: pytest.fail("not a graceful restart"))
    banner = DaemonStatusBanner()
    view = SettingsView(QThreadPool(), banner, lambda: None)
    qtbot.addWidget(view)
    view._on_daemon_action_failed(DaemonStartupError("down"), retry=view._restart)

    banner._action_button.click()

    qtbot.waitUntil(lambda: restarts == [True], timeout=2000)


def test_settings_view_retry_reruns_the_action_that_failed(qtbot, monkeypatch):
    from jarvis.modules.desktop_app.views import settings_view as settings_view_mod

    monkeypatch.setattr(settings_view_mod, "confirm_action", lambda *a, **k: True)
    calls = []

    def _force_restart():
        calls.append("force")
        raise RuntimeError("boom")

    monkeypatch.setattr(settings_view_mod.process_stats, "force_restart_daemon", _force_restart)
    monkeypatch.setattr(settings_view_mod, "_restart_and_respawn", lambda _id: calls.append("graceful"))
    banner = DaemonStatusBanner()
    view = SettingsView(QThreadPool(), banner, lambda: None)
    qtbot.addWidget(view)

    view._force_kill_button.click()
    qtbot.waitUntil(lambda: banner._action_button.text() == "Retry", timeout=2000)
    banner._action_button.click()

    qtbot.waitUntil(lambda: calls == ["force", "force"], timeout=2000)


def test_settings_view_prefills_the_shared_embedding_fields(qtbot, tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_DAEMON_HOME", str(tmp_path))
    env_config.save_settings("sk-existing", "a-model", "https://example.test", daemon_home=tmp_path)

    banner = DaemonStatusBanner()
    view = SettingsView(QThreadPool(), banner, lambda: None)
    qtbot.addWidget(view)

    assert view._api_key.text() == "sk-existing"
    assert view._model.text() == "a-model"
    assert view._api_base.text() == "https://example.test"


def test_settings_view_force_kill_without_confirmation_never_calls_force_kill_daemon(qtbot, monkeypatch):
    """Force Kill must be gated behind confirm_action - a stray click (or
    a future regression removing the confirmation) must not be able to
    SIGKILL the daemon."""
    from jarvis.modules.desktop_app.views import settings_view as settings_view_mod

    monkeypatch.setattr(settings_view_mod, "confirm_action", lambda *a, **k: False)
    killed = []
    monkeypatch.setattr(settings_view_mod.process_stats, "force_kill_daemon", lambda pid: killed.append(pid))

    banner = DaemonStatusBanner()
    view = SettingsView(QThreadPool(), banner, lambda: None)
    qtbot.addWidget(view)

    view._force_kill_button.click()

    assert killed == []


def test_settings_view_force_kill_confirmed_kills_then_respawns(qtbot, monkeypatch):
    from jarvis.modules.desktop_app.views import settings_view as settings_view_mod

    monkeypatch.setattr(settings_view_mod, "confirm_action", lambda *a, **k: True)
    monkeypatch.setattr(settings_view_mod.process_stats, "get_daemon_pid", lambda: 4321)
    killed = []
    monkeypatch.setattr(settings_view_mod.process_stats, "force_kill_daemon", lambda pid: killed.append(pid))
    respawned = []
    monkeypatch.setattr(
        settings_view_mod.process_stats.daemon_client, "ensure_daemon_running", lambda: respawned.append(True),
    )

    banner = DaemonStatusBanner()
    view = SettingsView(QThreadPool(), banner, lambda: None)
    qtbot.addWidget(view)

    view._force_kill_button.click()

    qtbot.waitUntil(lambda: killed == [4321], timeout=2000)
    assert respawned == [True]


def test_settings_view_restart_without_confirmation_never_calls_request_shutdown(qtbot, monkeypatch):
    from jarvis.modules.desktop_app.views import settings_view as settings_view_mod

    monkeypatch.setattr(settings_view_mod, "confirm_action", lambda *a, **k: False)
    shut_down = []
    monkeypatch.setattr(settings_view_mod, "request_shutdown", lambda client_id: shut_down.append(client_id))

    banner = DaemonStatusBanner()
    view = SettingsView(QThreadPool(), banner, lambda: "client-1")
    qtbot.addWidget(view)

    view._restart_button.click()

    assert shut_down == []


def test_settings_view_restart_confirmed_retires_the_current_client_then_respawns(qtbot, monkeypatch):
    from jarvis.modules.desktop_app.views import settings_view as settings_view_mod

    monkeypatch.setattr(settings_view_mod, "confirm_action", lambda *a, **k: True)
    shut_down = []
    monkeypatch.setattr(settings_view_mod, "request_shutdown", lambda client_id: shut_down.append(client_id))
    respawned = []
    monkeypatch.setattr(settings_view_mod, "ensure_daemon_running", lambda: respawned.append(True))

    banner = DaemonStatusBanner()
    view = SettingsView(QThreadPool(), banner, lambda: "client-1")
    qtbot.addWidget(view)

    view._restart_button.click()

    qtbot.waitUntil(lambda: shut_down == ["client-1"], timeout=2000)
    assert respawned == [True]


def test_settings_view_restart_confirm_names_the_action(qtbot, monkeypatch):
    from jarvis.modules.desktop_app.views import settings_view as settings_view_mod

    calls = []
    monkeypatch.setattr(
        settings_view_mod, "confirm_action",
        lambda *a, **k: calls.append((a, k)) or False,
    )

    banner = DaemonStatusBanner()
    view = SettingsView(QThreadPool(), banner, lambda: None)
    qtbot.addWidget(view)

    view._restart_button.click()

    assert calls[0][0][-1] == "Restart"


def test_settings_view_force_kill_confirm_names_the_action(qtbot, monkeypatch):
    from jarvis.modules.desktop_app.views import settings_view as settings_view_mod

    calls = []
    monkeypatch.setattr(
        settings_view_mod, "confirm_action",
        lambda *a, **k: calls.append((a, k)) or False,
    )

    banner = DaemonStatusBanner()
    view = SettingsView(QThreadPool(), banner, lambda: None)
    qtbot.addWidget(view)

    view._force_kill_button.click()

    assert calls[0][0][-1] == "Force kill"
    assert calls[0][1]["destructive"] is True


def test_settings_view_restart_button_shows_a_busy_label_while_restarting(qtbot, monkeypatch):
    from jarvis.modules.desktop_app.views import settings_view as settings_view_mod

    monkeypatch.setattr(settings_view_mod, "confirm_action", lambda *a, **k: True)
    monkeypatch.setattr(settings_view_mod, "request_shutdown", lambda client_id: None)
    release = threading.Event()
    monkeypatch.setattr(settings_view_mod, "ensure_daemon_running", lambda: release.wait(timeout=5))

    banner = DaemonStatusBanner()
    view = SettingsView(QThreadPool(), banner, lambda: "client-1")
    qtbot.addWidget(view)

    view._restart_button.click()

    assert view._restart_button.text() == "Restarting…"
    assert not view._restart_button.isEnabled()
    release.set()

    qtbot.waitUntil(lambda: view._restart_button.text() == "Restart", timeout=2000)
    assert view._restart_button.isEnabled()


def test_settings_view_restart_with_no_registered_client_skips_shutdown_but_still_respawns(qtbot, monkeypatch):
    """`get_daemon_client_id()` returns None whenever the daemon was never
    reached (or died before ever registering) - request_shutdown has
    nothing to retire in that case, matching MainWindow.closeEvent's own
    `if self._daemon_client_id is not None` guard."""
    from jarvis.modules.desktop_app.views import settings_view as settings_view_mod

    monkeypatch.setattr(settings_view_mod, "confirm_action", lambda *a, **k: True)

    def _boom(client_id):  # pragma: no cover - must never be called
        raise AssertionError("request_shutdown must not be called with no registered client")

    monkeypatch.setattr(settings_view_mod, "request_shutdown", _boom)
    respawned = []
    monkeypatch.setattr(settings_view_mod, "ensure_daemon_running", lambda: respawned.append(True))

    banner = DaemonStatusBanner()
    view = SettingsView(QThreadPool(), banner, lambda: None)
    qtbot.addWidget(view)

    view._restart_button.click()

    qtbot.waitUntil(lambda: respawned == [True], timeout=2000)


def test_settings_view_status_refresh_updates_ram_and_storage_labels_without_crashing(qtbot, monkeypatch):
    """RAM/storage display, driven entirely through mocked process_stats
    functions - no real daemon or psutil process needed beyond this
    test's own process (used for the app's own RAM reading)."""
    from jarvis.modules.desktop_app.views import settings_view as settings_view_mod

    monkeypatch.setattr(settings_view_mod.process_stats, "get_daemon_pid", lambda: 999)
    monkeypatch.setattr(settings_view_mod.process_stats, "get_process_ram", lambda pid: 50 * 1024 * 1024)
    monkeypatch.setattr(settings_view_mod.process_stats, "get_daemon_uptime_seconds", lambda pid: 125.0)
    monkeypatch.setattr(
        settings_view_mod.process_stats, "get_storage_usage",
        lambda: {"db_bytes": 100, "graph_bytes": 200, "total_bytes": 300},
    )

    banner = DaemonStatusBanner()
    view = SettingsView(QThreadPool(), banner, lambda: None)
    qtbot.addWidget(view)

    view._refresh_status()

    qtbot.waitUntil(lambda: view._daemon_status_label.text() == "Running", timeout=2000)
    assert "999" in view._daemon_pid_label.text()
    assert "2m 5s" in view._daemon_uptime_label.text()
    assert "50.0 MB" in view._daemon_ram_label.text()
    assert "MB" in view._app_ram_label.text() or "KB" in view._app_ram_label.text()
    assert "300" in view._storage_label.text() or "300.0 B" in view._storage_label.text()


def test_settings_view_status_refresh_shows_stopped_when_daemon_pid_is_none(qtbot, monkeypatch):
    from jarvis.modules.desktop_app.views import settings_view as settings_view_mod

    monkeypatch.setattr(settings_view_mod.process_stats, "get_daemon_pid", lambda: None)
    monkeypatch.setattr(
        settings_view_mod.process_stats, "get_storage_usage",
        lambda: {"db_bytes": 0, "graph_bytes": 0, "total_bytes": 0},
    )

    banner = DaemonStatusBanner()
    view = SettingsView(QThreadPool(), banner, lambda: None)
    qtbot.addWidget(view)

    view._refresh_status()

    qtbot.waitUntil(lambda: view._daemon_status_label.text() == "Stopped", timeout=2000)
    assert view._daemon_pid_label.text() == ""


def test_settings_view_save_requires_a_non_empty_api_key(qtbot, monkeypatch):
    monkeypatch.setattr(
        "jarvis.modules.desktop_app.views.settings_view.QMessageBox.warning",
        staticmethod(lambda *a, **k: None),
    )
    saved = []
    monkeypatch.setattr(
        "jarvis.modules.desktop_app.views.settings_view.env_config.save_settings",
        lambda *a, **k: saved.append(a),
    )

    banner = DaemonStatusBanner()
    view = SettingsView(QThreadPool(), banner, lambda: None)
    qtbot.addWidget(view)
    view._api_key.setText("")

    view._on_save_clicked()

    assert saved == []


def test_settings_view_save_persists_via_env_config(qtbot, tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_DAEMON_HOME", str(tmp_path))

    banner = DaemonStatusBanner()
    view = SettingsView(QThreadPool(), banner, lambda: None)
    qtbot.addWidget(view)
    view._api_key.setText("sk-typed-in")

    view._on_save_clicked()

    assert env_config.read_saved_settings(tmp_path)["EMBEDDING_API_KEY"] == "sk-typed-in"


def test_settings_view_save_offers_a_restart_service_action(qtbot, tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_DAEMON_HOME", str(tmp_path))

    banner = DaemonStatusBanner()
    view = SettingsView(QThreadPool(), banner, lambda: None)
    qtbot.addWidget(view)
    view._api_key.setText("sk-typed-in")

    view._on_save_clicked()

    assert banner.message() == "Saved. Restart the background service to apply."
    assert banner._action_button.text() == "Restart service"


def test_settings_view_check_for_updates_button_emits_signal(qtbot):
    banner = DaemonStatusBanner()
    view = SettingsView(QThreadPool(), banner, lambda: None)
    qtbot.addWidget(view)

    with qtbot.waitSignal(view.check_for_updates_clicked, timeout=1000):
        view._check_updates_button.click()


def test_settings_view_reveal_log_opens_its_containing_folder(qtbot, monkeypatch):
    from jarvis.modules.daemon import client as daemon_client

    opened = {}
    monkeypatch.setattr(
        "jarvis.modules.desktop_app.views.settings_view.QDesktopServices.openUrl",
        staticmethod(lambda url: opened.setdefault("url", url)),
    )

    banner = DaemonStatusBanner()
    view = SettingsView(QThreadPool(), banner, lambda: None)
    qtbot.addWidget(view)

    view._on_reveal_log_clicked()

    from pathlib import Path

    assert Path(opened["url"].toLocalFile()) == daemon_client.LOG_FILE.parent


def test_settings_view_polling_starts_on_show_and_stops_on_hide(qtbot, monkeypatch):
    from jarvis.modules.desktop_app.views import settings_view as settings_view_mod

    monkeypatch.setattr(
        settings_view_mod.process_stats, "get_daemon_pid", lambda: None,
    )
    monkeypatch.setattr(
        settings_view_mod.process_stats, "get_storage_usage",
        lambda: {"db_bytes": 0, "graph_bytes": 0, "total_bytes": 0},
    )

    banner = DaemonStatusBanner()
    view = SettingsView(QThreadPool(), banner, lambda: None)
    qtbot.addWidget(view)

    assert view._poll_timer.isActive() is False

    view.show()
    qtbot.waitExposed(view)

    assert view._poll_timer.isActive() is True

    view.hide()

    assert view._poll_timer.isActive() is False


def test_settings_view_model_and_api_base_live_in_the_advanced_section(qtbot):
    """Figma parity pass (Phase 10) - the real reference shows only an
    API key field in the primary view; Model/API base move into the
    collapsed-by-default Advanced section, not the main API key group."""
    banner = DaemonStatusBanner()
    view = SettingsView(QThreadPool(), banner, lambda: None)
    qtbot.addWidget(view)

    assert view._model.parent() is not None
    assert view._api_base.parent() is not None
    # Neither field's ancestor chain includes the main API key QGroupBox -
    # both should be reachable only by walking up through the
    # CollapsibleSection's own body widget instead.
    from jarvis.modules.desktop_app.collapsible_section import CollapsibleSection

    def _ancestor_is_a_collapsible_section(widget) -> bool:
        node = widget.parent()
        while node is not None:
            if isinstance(node, CollapsibleSection):
                return True
            node = node.parent()
        return False

    assert _ancestor_is_a_collapsible_section(view._model)
    assert _ancestor_is_a_collapsible_section(view._api_base)


def test_settings_view_save_persists_model_and_api_base_from_the_advanced_section(qtbot, tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_DAEMON_HOME", str(tmp_path))

    banner = DaemonStatusBanner()
    view = SettingsView(QThreadPool(), banner, lambda: None)
    qtbot.addWidget(view)
    view._api_key.setText("sk-typed-in")
    view._model.setText("a-different-model")
    view._api_base.setText("https://example.test/v9")

    view._on_save_clicked()

    saved = env_config.read_saved_settings(tmp_path)
    assert saved["EMBEDDING_MODEL"] == "a-different-model"
    assert saved["EMBEDDING_API_BASE"] == "https://example.test/v9"


def test_settings_view_claude_status_pill_reflects_connected(qtbot, monkeypatch):
    from jarvis.modules.desktop_app.views import settings_view as settings_view_mod

    monkeypatch.setattr(settings_view_mod.cds, "frozen_mcp_entry", lambda: {"command": "x", "args": ["--mcp"]})
    monkeypatch.setattr(
        settings_view_mod.cds, "build_plan",
        lambda: _claude_desktop_plan(cds.CASE_HAS_MCP_SERVERS, existing_jarvis_entry={"command": "x", "args": ["--mcp"]}),
    )

    banner = DaemonStatusBanner()
    view = SettingsView(QThreadPool(), banner, lambda: None)
    qtbot.addWidget(view)

    assert view._claude_status_pill.text() == "Connected"
    assert view._claude_status_pill.property("tagKind") == "connected"
    assert view._claude_status_label.text() == "Claude Desktop can use your files."
    assert view._claude_action_button.isHidden()  # nothing to do once it works


def test_settings_view_claude_status_pill_reflects_needs_repair(qtbot, monkeypatch):
    from jarvis.modules.desktop_app.views import settings_view as settings_view_mod

    monkeypatch.setattr(settings_view_mod.cds, "frozen_mcp_entry", lambda: {"command": "x", "args": ["--mcp"]})
    monkeypatch.setattr(
        settings_view_mod.cds, "build_plan",
        lambda: _claude_desktop_plan(cds.CASE_HAS_MCP_SERVERS, existing_jarvis_entry={"command": "stale", "args": []}),
    )

    banner = DaemonStatusBanner()
    view = SettingsView(QThreadPool(), banner, lambda: None)
    qtbot.addWidget(view)

    assert view._claude_status_pill.text() == "Needs repair"
    assert view._claude_status_pill.property("tagKind") == "needs_repair"
    assert view._claude_action_button.text() == "Repair"
    assert not view._claude_action_button.isHidden()


def test_settings_view_offers_connect_not_repair_when_claude_is_not_connected(qtbot, monkeypatch):
    from jarvis.modules.desktop_app.views import settings_view as settings_view_mod

    monkeypatch.setattr(settings_view_mod.cds, "frozen_mcp_entry", lambda: {"command": "x", "args": ["--mcp"]})
    monkeypatch.setattr(settings_view_mod.cds, "build_plan", lambda: _claude_desktop_plan(cds.CASE_MISSING))

    view = SettingsView(QThreadPool(), DaemonStatusBanner(), lambda: None)
    qtbot.addWidget(view)

    assert view._claude_status_pill.text() == "Not connected"
    assert view._claude_action_button.text() == "Connect"


def test_settings_view_claude_status_pill_reflects_not_connected(qtbot, monkeypatch):
    from jarvis.modules.desktop_app.views import settings_view as settings_view_mod

    monkeypatch.setattr(settings_view_mod.cds, "build_plan", lambda: _claude_desktop_plan(cds.CASE_MISSING))

    banner = DaemonStatusBanner()
    view = SettingsView(QThreadPool(), banner, lambda: None)
    qtbot.addWidget(view)

    assert view._claude_status_pill.text() == "Not connected"
    assert view._claude_status_pill.property("tagKind") == "not_connected"


def test_settings_view_updates_label_before_any_check_shows_just_the_version(qtbot):
    banner = DaemonStatusBanner()
    view = SettingsView(QThreadPool(), banner, lambda: None)
    qtbot.addWidget(view)

    assert view._updates_label.text() == view._version_text


def test_settings_view_report_update_check_result_shows_checked_time(qtbot):
    banner = DaemonStatusBanner()
    view = SettingsView(QThreadPool(), banner, lambda: None)
    qtbot.addWidget(view)

    view.report_update_check_result(False)

    assert "You're up to date." in view._updates_label.text()
    assert "just now" in view._updates_label.text()


def test_settings_view_report_update_check_result_shows_update_available(qtbot):
    banner = DaemonStatusBanner()
    view = SettingsView(QThreadPool(), banner, lambda: None)
    qtbot.addWidget(view)

    view.report_update_check_result(True)

    assert "An update is available." in view._updates_label.text()


# ---------------------------------------------------------------------
# StatsActivityView (plan §9) - processing stats summary + per-extension
# table, and paginated tool-call history. Driven directly with
# constructed model objects, matching this file's existing pattern for
# SearchView/HierarchyView result handling.
# ---------------------------------------------------------------------

def _tool_call(id_: int, *, success: bool = True) -> ToolCallRecord:
    return ToolCallRecord(
        id=id_, call_type="search", source="app", request="{}", response="{}",
        success=success, started_at=f"2026-01-01T00:{id_:02d}:00",
    )


def test_stats_activity_view_instantiates(qtbot):
    banner = DaemonStatusBanner()
    view = StatsActivityView(QThreadPool(), banner)
    qtbot.addWidget(view)

    assert view._file_type_tree.topLevelItemCount() == 0
    assert view._tool_calls_tree.topLevelItemCount() == 0
    assert view._load_more_button.isHidden() is True


def test_stats_activity_view_renders_processing_stats_summary_and_table(qtbot):
    banner = DaemonStatusBanner()
    view = StatsActivityView(QThreadPool(), banner)
    qtbot.addWidget(view)

    result = FileTypeStatsResult(
        totals=ProcessingTotals(total_count=5, total_bytes=1024 * 1024),
        by_extension=[
            FileTypeStat(
                extension=".md", supported=True, tried_count=3, skipped_count=1,
                failed_count=0, ignored_count=0, bytes_processed=1024,
                last_updated_at="2026-01-01T00:00:00",
            ),
        ],
    )

    view._on_file_type_stats_loaded(result)

    assert "5 files processed" in view._processing_summary_label.text()
    assert view._file_type_tree.topLevelItemCount() == 1
    item = view._file_type_tree.topLevelItem(0)
    assert item.text(0) == ".md"
    assert item.text(1) == "3"
    assert item.text(3) == "0"


def test_stats_activity_view_uses_plain_column_names_and_a_dash_for_no_size(qtbot):
    view = StatsActivityView(QThreadPool(), DaemonStatusBanner())
    qtbot.addWidget(view)
    header = view._file_type_tree.headerItem()

    assert [header.text(i) for i in range(header.columnCount())] == [
        "File type", "Read", "Skipped", "Couldn't read", "Not supported", "Size",
    ]

    view._on_file_type_stats_loaded(FileTypeStatsResult(
        totals=ProcessingTotals(total_count=1, total_bytes=0),
        by_extension=[FileTypeStat(
            extension=".png", supported=False, tried_count=0, skipped_count=0,
            failed_count=0, ignored_count=1, bytes_processed=0, last_updated_at="2026-01-01T00:00:00",
        )],
    ))

    assert view._file_type_tree.topLevelItem(0).text(5) == "—"


def test_stats_activity_view_pagination_matches_search_wording(qtbot):
    view = StatsActivityView(QThreadPool(), DaemonStatusBanner())
    search = SearchView(QThreadPool(), DaemonStatusBanner())
    qtbot.addWidget(view)
    qtbot.addWidget(search)

    assert view._load_more_button.text() == search._load_more_button.text() == "Load more"


def test_stats_activity_view_shows_banner_on_processing_stats_error(qtbot):
    banner = DaemonStatusBanner()
    view = StatsActivityView(QThreadPool(), banner)
    qtbot.addWidget(view)
    qtbot.addWidget(banner)

    view._on_file_type_stats_loaded(ErrorResponse(error=ErrorDetail(code="daemon_unavailable", message="nope")))

    assert banner.isVisible() is True


def test_stats_activity_view_shows_banner_on_tool_call_history_error(qtbot):
    banner = DaemonStatusBanner()
    view = StatsActivityView(QThreadPool(), banner)
    qtbot.addWidget(view)
    qtbot.addWidget(banner)

    view._on_tool_call_history_loaded(
        ErrorResponse(error=ErrorDetail(code="daemon_unavailable", message="nope")), append=False,
    )

    assert banner.isVisible() is True


def test_stats_activity_view_loads_a_full_page_and_tracks_the_oldest_id(qtbot):
    banner = DaemonStatusBanner()
    view = StatsActivityView(QThreadPool(), banner)
    qtbot.addWidget(view)

    calls = [_tool_call(i) for i in range(50, 0, -1)]  # newest-first, ids 50..1
    view._on_tool_call_history_loaded(ToolCallHistoryResult(calls=calls), append=False)

    assert view._tool_calls_tree.topLevelItemCount() == 50
    assert view._oldest_call_id_shown == 1
    assert view._load_more_button.isHidden() is False  # a full page - more may exist


def test_stats_activity_view_short_page_hides_load_more(qtbot):
    banner = DaemonStatusBanner()
    view = StatsActivityView(QThreadPool(), banner)
    qtbot.addWidget(view)

    view._on_tool_call_history_loaded(ToolCallHistoryResult(calls=[_tool_call(1)]), append=False)

    assert view._load_more_button.isHidden() is True


def test_stats_activity_view_time_column_reads_relative_not_raw_iso(qtbot):
    """Confirmed real bug, flagged during the Figma parity pass (Phase 9):
    the Time column used to show the backend's raw ISO timestamp
    (`call.started_at`) verbatim, clipped by the column width to
    something unreadable like "2026-01-...". Now runs through
    format_utils.format_relative_time, same as Search's result rows."""
    banner = DaemonStatusBanner()
    view = StatsActivityView(QThreadPool(), banner)
    qtbot.addWidget(view)

    view._on_tool_call_history_loaded(ToolCallHistoryResult(calls=[_tool_call(1)]), append=False)

    time_text = view._tool_calls_tree.topLevelItem(0).text(0)
    assert "T" not in time_text
    assert time_text != _tool_call(1).started_at


def test_stats_activity_view_load_more_appends_and_advances_the_cursor(qtbot, monkeypatch):
    """The real pagination path, through run_daemon_call ->
    daemon_client.get_tool_call_history - confirms Next Page passes the
    oldest id already shown as `before_id`, and appends (not replaces)
    the next page's rows on top of the first."""
    from jarvis.modules.desktop_app.views import stats_activity_view as stats_activity_view_mod

    banner = DaemonStatusBanner()
    view = StatsActivityView(QThreadPool(), banner)
    qtbot.addWidget(view)

    first_page = [_tool_call(i) for i in range(50, 0, -1)]
    view._on_tool_call_history_loaded(ToolCallHistoryResult(calls=first_page), append=False)
    assert view._oldest_call_id_shown == 1

    captured = {}

    def _fake_get_tool_call_history(cursor=None, limit=50):
        captured["cursor"] = cursor
        return ToolCallHistoryResult(calls=[_tool_call(0, success=False)])

    monkeypatch.setattr(
        stats_activity_view_mod.daemon_client, "get_tool_call_history", _fake_get_tool_call_history,
    )

    view._load_more_button.click()

    qtbot.waitUntil(lambda: view._tool_calls_tree.topLevelItemCount() == 51, timeout=2000)
    assert captured["cursor"] == 1
    assert view._oldest_call_id_shown == 0
    assert view._load_more_button.isHidden() is True


def test_stats_activity_view_tool_call_row_shows_the_result_as_done_or_failed(qtbot):
    banner = DaemonStatusBanner()
    view = StatsActivityView(QThreadPool(), banner)
    qtbot.addWidget(view)

    view._on_tool_call_history_loaded(
        ToolCallHistoryResult(calls=[_tool_call(1, success=True), _tool_call(2, success=False)]), append=False,
    )

    texts = {view._tool_calls_tree.topLevelItem(i).text(2) for i in range(2)}
    assert texts == {"Done", "Failed"}


def test_stats_activity_view_rows_say_what_happened_in_plain_words(qtbot):
    from jarvis.modules.desktop_app.views.stats_activity_view import _activity_label

    assert _activity_label("mcp_tool", "search") == "Searched your files"
    assert _activity_label("llm", "cluster_label") == "Named a topic"
    assert _activity_label("mcp_tool", "something_new") == "Other activity"

    banner = DaemonStatusBanner()
    view = StatsActivityView(QThreadPool(), banner)
    qtbot.addWidget(view)
    view._on_tool_call_history_loaded(ToolCallHistoryResult(calls=[_tool_call(1)]), append=False)

    row = [view._tool_calls_tree.topLevelItem(0).text(i) for i in range(3)]
    assert not any(raw in text for text in row for raw in ("mcp_tool", "llm", "_"))
    assert view._tool_calls_tree.rootIsDecorated() is False


# ---------------------------------------------------------------------
# MainWindow wiring for the two new pages - replacing the old placeholder
# widgets with real views, and connecting settings_view's update-check
# signal to MainWindow's own (unchanged) update state machine.
# ---------------------------------------------------------------------

def test_main_window_settings_and_stats_pages_are_the_real_views(qtbot, monkeypatch):
    from jarvis.modules.desktop_app import main_window as main_window_mod

    monkeypatch.setattr(main_window_mod, "run_daemon_call", lambda *a, **k: None)

    window = MainWindow(QThreadPool())
    qtbot.addWidget(window)

    assert isinstance(window.settings_view, SettingsView)
    assert isinstance(window.stats_view, StatsActivityView)


def test_main_window_builds_five_pages_wired_to_nav_rail_by_position(qtbot, monkeypatch):
    """Regression for the DESIGN.md Phase 2 sidebar work: NavRail now has
    text labels and a Collapse control, but the page-order wiring is
    still purely positional (NAV_ITEMS order <-> QStackedWidget index,
    no name-based mapping) - confirms that didn't quietly break."""
    from jarvis.modules.desktop_app import main_window as main_window_mod

    monkeypatch.setattr(main_window_mod, "run_daemon_call", lambda *a, **k: None)

    window = MainWindow(QThreadPool())
    qtbot.addWidget(window)

    assert window.pages.count() == 5
    assert window.pages.widget(0) is window.connectors_view
    assert window.pages.widget(4) is window.settings_view

    window.nav_rail.page_selected.emit(4)
    assert window.pages.currentWidget() is window.settings_view


def test_main_window_go_to_sources_switches_page_and_nav_selection(qtbot, monkeypatch):
    """DESIGN.md §4.8's "Go to Sources" action (Phase 6) - no view could
    trigger a page switch before this; confirms the new mechanism keeps
    both the QStackedWidget and the nav rail's own selected button in
    sync, not just one of the two."""
    from jarvis.modules.desktop_app import main_window as main_window_mod

    monkeypatch.setattr(main_window_mod, "run_daemon_call", lambda *a, **k: None)

    window = MainWindow(QThreadPool())
    qtbot.addWidget(window)
    window.pages.setCurrentIndex(1)  # Search

    window.search_view.go_to_sources_clicked.emit()

    assert window.pages.currentWidget() is window.connectors_view
    assert window.nav_rail._nav_buttons[0].isChecked() is True


def test_main_window_go_to_search_switches_page_and_nav_selection(qtbot, monkeypatch):
    """DESIGN.md §6's "Search your files" action on a finished sync -
    the same cross-view page-switch mechanism as "Go to Sources",
    pointed the other direction."""
    from jarvis.modules.desktop_app import main_window as main_window_mod

    monkeypatch.setattr(main_window_mod, "run_daemon_call", lambda *a, **k: None)

    window = MainWindow(QThreadPool())
    qtbot.addWidget(window)

    window.connectors_view.go_to_search_clicked.emit()

    assert window.pages.currentWidget() is window.search_view
    assert window.nav_rail._nav_buttons[1].isChecked() is True


def test_main_window_settings_view_check_for_updates_signal_triggers_manual_check(qtbot, monkeypatch):
    from jarvis.modules.desktop_app import main_window as main_window_mod

    monkeypatch.setattr(main_window_mod, "run_daemon_call", lambda *a, **k: None)
    monkeypatch.setattr(main_window_mod.version, "get_current_version", lambda: "desktop-build-2026-01-01")
    monkeypatch.setattr(main_window_mod.update_checker, "run_update_check", lambda *a, **k: None)

    window = MainWindow(QThreadPool())
    qtbot.addWidget(window)

    calls = []
    monkeypatch.setattr(window, "_check_for_updates", lambda manual: calls.append(manual))

    window.settings_view.check_for_updates_clicked.emit()

    assert calls == [True]


# ---------------------------------------------------------------------
# Telemetry opt-out (docs/observability-DESIGN.md §9): the Settings
# toggle.
# ---------------------------------------------------------------------

def test_settings_privacy_toggle_saves_the_choice(qtbot, monkeypatch):
    from jarvis.modules.desktop_app.views import settings_view as settings_view_mod

    monkeypatch.setattr(settings_view_mod.env_config, "telemetry_enabled", lambda: True)
    saved = []
    monkeypatch.setattr(settings_view_mod.env_config, "save_telemetry_enabled", saved.append)
    banner = DaemonStatusBanner()
    view = SettingsView(QThreadPool(), banner, lambda: None)
    qtbot.addWidget(view)

    assert view._telemetry_checkbox.isChecked()
    assert view._telemetry_checkbox.text() == "Share usage and diagnostics"
    view._telemetry_checkbox.setChecked(False)

    assert saved == [False]
    assert "stop sharing" in banner.message()


def test_settings_privacy_toggle_reverts_if_the_choice_cannot_be_saved(qtbot, monkeypatch):
    from jarvis.modules.desktop_app.views import settings_view as settings_view_mod

    monkeypatch.setattr(settings_view_mod.env_config, "telemetry_enabled", lambda: True)

    def _fail(enabled):
        raise OSError("read-only")

    monkeypatch.setattr(settings_view_mod.env_config, "save_telemetry_enabled", _fail)
    banner = DaemonStatusBanner()
    view = SettingsView(QThreadPool(), banner, lambda: None)
    qtbot.addWidget(view)

    view._telemetry_checkbox.setChecked(False)

    assert view._telemetry_checkbox.isChecked()
    assert banner.property("bannerKind") == "error"


# ---------------------------------------------------------------------
# Removing a source (Sources row "Remove")
# ---------------------------------------------------------------------

def _run_daemon_call_inline(pool, fn, *args, on_success=None, on_error=None, **kwargs):
    try:
        result = fn(*args, **kwargs)
    except Exception as exc:
        if on_error:
            on_error(exc)
        return
    if on_success:
        on_success(result)


def _removal_view(qtbot, monkeypatch, remove_result, confirmed=True):
    from jarvis.modules.desktop_app.views import connectors_view as connectors_view_mod

    _no_modal_dialogs(monkeypatch)
    calls = {"confirm": [], "remove": [], "reloads": 0}

    def _confirm(parent, title, message, action_label, *, destructive=False):
        calls["confirm"].append((title, message, action_label, destructive))
        return confirmed

    def _remove(connector, root_id):
        calls["remove"].append((connector, root_id))
        if isinstance(remove_result, Exception):
            raise remove_result
        return remove_result

    monkeypatch.setattr(connectors_view_mod, "confirm_action", _confirm)
    monkeypatch.setattr(connectors_view_mod, "run_daemon_call", _run_daemon_call_inline)
    monkeypatch.setattr(connectors_view_mod.daemon_client, "remove_sync_root", _remove)
    view = ConnectorsView(QThreadPool())
    qtbot.addWidget(view)
    monkeypatch.setattr(view, "_load_synced_roots", lambda: calls.__setitem__("reloads", calls["reloads"] + 1))
    return view, calls


def test_removing_a_source_confirms_first_then_shows_success_and_reloads(qtbot, monkeypatch):
    view, calls = _removal_view(qtbot, monkeypatch, {"removed": 3})
    root = _sync_root("filesystem", _p("x", "jarvis-demo"))

    view._on_remove_clicked(root)

    assert calls["confirm"] == [(
        "Remove “jarvis-demo”?",
        "Its files will no longer appear in search. Your files themselves aren't changed.",
        "Remove", True,
    )]
    assert calls["remove"] == [("filesystem", root.root_id)]
    assert _banner_state(view) == {
        "text": "Removed “jarvis-demo”. Its files no longer appear in search.",
        "kind": "success", "action": None, "secondary": None,
    }
    assert calls["reloads"] == 1


def test_cancelling_the_remove_confirm_changes_nothing(qtbot, monkeypatch):
    view, calls = _removal_view(qtbot, monkeypatch, {"removed": 3}, confirmed=False)

    view._on_remove_clicked(_sync_root("filesystem", _p("x", "jarvis-demo")))

    assert calls["remove"] == []
    assert view._status_banner.isHidden()


def test_a_refused_removal_shows_the_service_message_with_try_again(qtbot, monkeypatch):
    message = "A sync is still running. Remove this source once it's done."
    view, calls = _removal_view(
        qtbot, monkeypatch, {"error": {"code": "ingest_in_progress", "message": message}},
    )

    view._on_remove_clicked(_sync_root("filesystem", _p("x", "jarvis-demo")))

    assert _banner_state(view) == {"text": message, "kind": "error", "action": "Try again", "secondary": None}
    assert calls["reloads"] == 0
    view._status_banner._action_button.click()
    assert len(calls["remove"]) == 2  # Try again goes straight to the removal, no second confirm
    assert len(calls["confirm"]) == 1


def test_a_drive_removal_needing_sign_in_offers_reconnect_drive(qtbot, monkeypatch):
    view, _calls = _removal_view(
        qtbot, monkeypatch,
        {"error": {"code": "drive_auth_expired", "message": "Google Drive needs you to sign in again."}},
    )

    view._on_remove_clicked(_sync_root("drive", "folder-abc"))

    assert _banner_state(view)["action"] == "Reconnect Drive"


def test_a_removal_with_the_service_unreachable_shows_a_calm_banner(qtbot, monkeypatch):
    from jarvis.modules.daemon.client import DaemonConnectionError

    view, _calls = _removal_view(qtbot, monkeypatch, DaemonConnectionError("unreachable"))

    view._on_remove_clicked(_sync_root("filesystem", _p("x", "jarvis-demo")))

    assert _banner_state(view) == {
        "text": "The background service isn't running. Everything already saved is safe.",
        "kind": "error", "action": "Try again", "secondary": None,
    }
    assert view._synced_roots_list.isEnabled()
