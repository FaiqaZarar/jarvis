"""Tests for the Sources row redesign (DESIGN.md §4.6) in isolation -
the pure ordering/state helpers and SyncedRootRow's own rendering, not
ConnectorsView's wiring (that's test_desktop_app_views.py, same split
test_desktop_app_setup_checklist.py already keeps)."""

from jarvis.modules.desktop_app.views.connectors_view import (
    SyncedRootRow,
    _order_synced_roots,
    _row_display_name,
    _row_sync_state,
)
from jarvis_server.mcp_tools.models import SyncRootRecord


def _root(root_id: str, connector: str = "filesystem") -> SyncRootRecord:
    return SyncRootRecord(
        connector=connector, root_id=root_id,
        first_synced_at="2026-01-01T00:00:00", last_synced_at="2026-01-01T00:00:00",
    )


def test_row_display_name_uses_basename_for_filesystem():
    import os
    root = _root(os.sep + os.sep.join(["watched", "notes"]))
    assert _row_display_name(root) == "notes"


def test_row_display_name_uses_the_drive_folder_name_never_its_id():
    named = _root("folder-abc", connector="drive").model_copy(update={"display_name": "Team docs"})
    assert _row_display_name(named) == "Team docs"
    assert _row_display_name(_root("folder-abc", connector="drive")) == "Google Drive folder"


def test_order_synced_roots_floats_syncing_root_first_then_alphabetical():
    zebra = _root("zebra")
    apple = _root("apple")
    mango = _root("mango")

    ordered = _order_synced_roots([zebra, apple, mango], syncing_root_id="mango")

    assert [r.root_id for r in ordered] == ["mango", "apple", "zebra"]


def test_order_synced_roots_is_purely_alphabetical_when_nothing_is_syncing():
    ordered = _order_synced_roots([_root("zebra"), _root("apple")], syncing_root_id="")

    assert [r.root_id for r in ordered] == ["apple", "zebra"]


def test_row_sync_state_truth_table():
    assert _row_sync_state("a", "a", is_polling=False, is_cancelling=False) == "synced"
    assert _row_sync_state("a", "a", is_polling=False, is_cancelling=True) == "synced"
    assert _row_sync_state("a", "b", is_polling=True, is_cancelling=False) == "busy"
    assert _row_sync_state("a", "a", is_polling=True, is_cancelling=False) == "syncing"
    assert _row_sync_state("a", "a", is_polling=True, is_cancelling=True) == "cancelling"


def test_row_shows_name_secondary_tag_and_sync_now_in_synced_state(qtbot):
    row = SyncedRootRow(_root("/x/watched"), on_sync_now=lambda: None, on_cancel=lambda: None, on_remove=lambda: None)
    qtbot.addWidget(row)

    assert row._name_label.text() == "watched"
    assert row._secondary_label.text() == "/x/watched · 1 Jan"
    assert row._tag.text() == "Synced"
    assert row._tag.property("tagKind") == "synced"
    assert row._action_button.text() == "Sync now"
    assert row._action_button.isEnabled() is True


def test_row_icon_differs_between_filesystem_and_drive(qtbot):
    """Figma parity pass (Phase 9) - a per-connector icon chip, not just
    text. Compared by rendered pixel content (QIcon has no equality
    operator worth relying on) against a fresh render of each source
    SVG - confirms filesystem gets the folder glyph, Drive gets the
    cloud glyph, and the two are genuinely different images."""
    from jarvis.modules.desktop_app import theme

    fs_row = SyncedRootRow(_root("/x/watched", connector="filesystem"), on_sync_now=lambda: None, on_cancel=lambda: None, on_remove=lambda: None)
    qtbot.addWidget(fs_row)
    drive_row = SyncedRootRow(_root("folder-abc", connector="drive"), on_sync_now=lambda: None, on_cancel=lambda: None, on_remove=lambda: None)
    qtbot.addWidget(drive_row)

    folder_pixmap = theme.icon_from_svg(theme.FOLDER_SVG, theme.TEXT_SECONDARY).pixmap(20, 20)
    cloud_pixmap = theme.icon_from_svg(theme.CLOUD_SVG, theme.TEXT_SECONDARY).pixmap(20, 20)

    assert fs_row._icon_label.pixmap().toImage() == folder_pixmap.toImage()
    assert drive_row._icon_label.pixmap().toImage() == cloud_pixmap.toImage()
    assert fs_row._icon_label.pixmap().toImage() != cloud_pixmap.toImage()


def test_row_shows_cancel_and_progress_text_in_syncing_state(qtbot):
    row = SyncedRootRow(_root("/x/watched"), on_sync_now=lambda: None, on_cancel=lambda: None, on_remove=lambda: None)
    qtbot.addWidget(row)

    row.update_state("syncing", progress_text="Reading files, 3 of 10", fraction=0.3)

    assert row._progress_panel.isHidden() is False
    assert row._progress_text.text() == "Reading files, 3 of 10"
    assert row._progress_bar.value() == 300
    assert row._tag.text() == "Syncing"
    assert row._tag.property("tagKind") == "syncing"
    assert row._action_button.text() == "Cancel"
    assert row._action_button.isEnabled() is True
    assert row._action_button.property("buttonKind") == "quiet"


def test_row_shows_cancelling_text_and_disables_action(qtbot):
    row = SyncedRootRow(_root("/x/watched"), on_sync_now=lambda: None, on_cancel=lambda: None, on_remove=lambda: None)
    qtbot.addWidget(row)

    row.update_state("cancelling")

    assert row._progress_panel.isHidden() is False
    assert row._progress_text.text().startswith("Cancelling.")
    assert row._action_button.text() == "Cancel"
    assert row._action_button.isEnabled() is False


def test_row_disables_action_but_keeps_synced_tag_in_busy_state(qtbot):
    row = SyncedRootRow(_root("/x/watched"), on_sync_now=lambda: None, on_cancel=lambda: None, on_remove=lambda: None)
    qtbot.addWidget(row)

    row.update_state("busy")

    assert row._tag.text() == "Synced"
    assert row._action_button.text() == "Sync now"
    assert row._action_button.isEnabled() is False


def test_clicking_action_calls_on_sync_now_when_idle(qtbot):
    calls = []
    row = SyncedRootRow(_root("/x/watched"), on_sync_now=lambda: calls.append("sync"), on_cancel=lambda: calls.append("cancel"), on_remove=lambda: calls.append("remove"))
    qtbot.addWidget(row)

    row._action_button.click()

    assert calls == ["sync"]


def test_clicking_action_calls_on_cancel_when_syncing(qtbot):
    calls = []
    row = SyncedRootRow(_root("/x/watched"), on_sync_now=lambda: calls.append("sync"), on_cancel=lambda: calls.append("cancel"), on_remove=lambda: calls.append("remove"))
    qtbot.addWidget(row)
    row.update_state("syncing")

    row._action_button.click()

    assert calls == ["cancel"]


def test_row_returns_to_sync_now_after_syncing_ends(qtbot):
    row = SyncedRootRow(_root("/x/watched"), on_sync_now=lambda: None, on_cancel=lambda: None, on_remove=lambda: None)
    qtbot.addWidget(row)
    row.update_state("syncing", progress_text="…")

    row.update_state("synced")

    assert row._action_button.text() == "Sync now"
    assert row._action_button.isEnabled() is True
    assert row._action_button.property("buttonKind") == ""
    assert row._secondary_label.text() == "/x/watched · 1 Jan"


def test_row_progress_bar_is_indeterminate_without_a_fraction(qtbot):
    row = SyncedRootRow(_root("/x/watched"), on_sync_now=lambda: None, on_cancel=lambda: None, on_remove=lambda: None)
    qtbot.addWidget(row)

    row.update_state("syncing", progress_text="Organizing…")

    assert row._progress_bar.maximum() == 0


def test_row_hides_progress_panel_once_no_longer_syncing(qtbot):
    row = SyncedRootRow(_root("/x/watched"), on_sync_now=lambda: None, on_cancel=lambda: None, on_remove=lambda: None)
    qtbot.addWidget(row)
    row.update_state("syncing", progress_text="x", fraction=0.5)

    row.update_state("synced")

    assert row._progress_panel.isHidden() is True



def test_remove_shows_only_while_nothing_is_syncing(qtbot):
    row = SyncedRootRow(_root("/x/watched"), on_sync_now=lambda: None, on_cancel=lambda: None, on_remove=lambda: None)
    qtbot.addWidget(row)

    visible = {}
    for state in ("synced", "syncing", "cancelling", "busy"):
        row.update_state(state)
        visible[state] = not row._remove_button.isHidden()

    assert visible == {"synced": True, "syncing": False, "cancelling": False, "busy": False}


def test_clicking_remove_calls_on_remove(qtbot):
    calls = []
    row = SyncedRootRow(_root("/x/watched"), on_sync_now=lambda: calls.append("sync"), on_cancel=lambda: calls.append("cancel"), on_remove=lambda: calls.append("remove"))
    qtbot.addWidget(row)
    row.update_state("synced")

    row._remove_button.click()

    assert calls == ["remove"]

def test_progress_fraction():
    from jarvis.modules.desktop_app.views.connectors_view import _progress_fraction

    assert _progress_fraction("Reading files", {"processed": 310, "total": 500}) == 0.62
    assert _progress_fraction("Reading files", {"processed": 5, "total": None}) is None
    assert _progress_fraction("Reading files", None) is None
    assert _progress_fraction("Reconciling", {"processed": 1, "total": 2}) is None


def test_with_nesting_depth_puts_children_right_after_their_parent():
    import os
    from jarvis.modules.desktop_app.views.connectors_view import _with_nesting_depth

    parent = _root(os.sep + "w")
    child = _root(os.sep + os.sep.join(["w", "n"]))
    other = _root("folder", connector="drive")

    flattened = _with_nesting_depth([parent, other, child])

    assert [(r.root_id, d) for r, d in flattened] == [(parent.root_id, 0), (child.root_id, 1), ("folder", 0)]
