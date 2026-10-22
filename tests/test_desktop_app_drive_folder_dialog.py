"""DriveFolderDialog (docs/desktop-app-drive-connector-DESIGN.md §3b) -
the lazy-expand-on-click folder browser. Driven the same way
HierarchyView's own tests are: real widget construction via
qtbot.addWidget, internal handler methods called directly with
constructed data, `confirm_action` monkeypatched (on this module's own
imported name, not `QMessageBox` itself - Phase 7 replaced the raw
`QMessageBox.question()` call with the shared `confirm_action()` helper)
so a confirmation step never blocks the test on a real modal.
"""

from PySide6.QtCore import QThreadPool
from PySide6.QtWidgets import QDialog

from jarvis.modules.desktop_app.drive_folder_dialog import DriveFolderDialog


def _folder(id_, name):
    return {"id": id_, "name": name}


def test_dialog_starts_with_a_single_root_item_and_a_loading_placeholder(qtbot):
    dialog = DriveFolderDialog(object(), QThreadPool())
    qtbot.addWidget(dialog)

    assert dialog._tree.topLevelItemCount() == 1
    root = dialog._tree.topLevelItem(0)
    assert root.text(0) == "My Drive"
    assert root.childCount() == 1
    assert root.child(0).text(0) == "Loading…"


def test_expanding_a_node_replaces_the_placeholder_with_real_folders(qtbot):
    dialog = DriveFolderDialog(object(), QThreadPool())
    qtbot.addWidget(dialog)
    root = dialog._tree.topLevelItem(0)

    dialog._on_folders_loaded(root, [_folder("f1", "Reports"), _folder("f2", "Archive")])

    assert root.childCount() == 2
    names = {root.child(i).text(0) for i in range(2)}
    assert names == {"Reports", "Archive"}
    # Each real folder gets its own "Loading…" placeholder so it draws
    # as expandable too - unlike HierarchyView's topics, list_drive_folders
    # gives no upfront hint about whether a folder has sub-folders.
    for i in range(2):
        child = root.child(i)
        assert child.childCount() == 1
        assert child.child(0).text(0) == "Loading…"


def test_a_folder_with_no_subfolders_ends_up_with_no_children_at_all(qtbot):
    """The empty-listing case - confirms the placeholder is genuinely
    removed (not left behind), which is also what makes Qt stop
    drawing an expand arrow for it."""
    dialog = DriveFolderDialog(object(), QThreadPool())
    qtbot.addWidget(dialog)
    root = dialog._tree.topLevelItem(0)

    dialog._on_folders_loaded(root, [])

    assert root.childCount() == 0


def test_expanding_the_same_node_twice_only_fetches_once(qtbot, monkeypatch):
    """Mirrors HierarchyView's own tested dedup via _loaded_topic_ids -
    _loaded_folder_ids here serves the identical purpose."""
    from jarvis.modules.desktop_app import drive_folder_dialog as mod

    calls = []
    monkeypatch.setattr(
        mod.drive, "list_drive_folders",
        lambda service, parent_id: (calls.append(parent_id), [_folder("f1", "Reports")])[1],
    )

    dialog = DriveFolderDialog(object(), QThreadPool())
    qtbot.addWidget(dialog)
    root = dialog._tree.topLevelItem(0)

    dialog._on_item_expanded(root)
    qtbot.waitUntil(lambda: root.childCount() == 1 and root.child(0).text(0) == "Reports", timeout=2000)
    dialog._on_item_expanded(root)

    qtbot.wait(200)  # let a wrongly-fired second fetch have a chance to land
    assert calls == ["root"]


def test_a_listing_failure_removes_the_placeholder_and_the_loaded_marker(qtbot, monkeypatch):
    """A failed expand must not leave the node stuck as "already loaded"
    forever (it's discarded from _loaded_folder_ids) or with a stale
    "Loading…" child sitting there once the error dialog is dismissed."""
    from jarvis.modules.desktop_app import drive_folder_dialog as mod

    monkeypatch.setattr(mod, "QMessageBox", type("M", (), {"critical": staticmethod(lambda *a, **k: None)}))

    dialog = DriveFolderDialog(object(), QThreadPool())
    qtbot.addWidget(dialog)
    root = dialog._tree.topLevelItem(0)
    folder_id = root.data(0, mod._FOLDER_ID_ROLE)
    dialog._loaded_folder_ids.add(folder_id)

    dialog._on_listing_failed(root, RuntimeError("boom"))

    assert root.childCount() == 0
    assert folder_id not in dialog._loaded_folder_ids


def test_clicking_an_item_enables_select_and_updates_the_breadcrumb(qtbot):
    dialog = DriveFolderDialog(object(), QThreadPool())
    qtbot.addWidget(dialog)
    root = dialog._tree.topLevelItem(0)
    dialog._on_folders_loaded(root, [_folder("f1", "Reports")])
    child = root.child(0)

    assert dialog._select_button.isEnabled() is False

    dialog._on_item_clicked(child, 0)

    assert dialog._select_button.isEnabled() is True
    assert dialog._breadcrumb.text() == "My Drive / Reports"


def test_confirming_the_scanned_preview_sets_selection_and_accepts(qtbot, monkeypatch):
    from jarvis.modules.desktop_app import drive_folder_dialog as drive_folder_dialog_mod

    calls = []
    monkeypatch.setattr(
        drive_folder_dialog_mod, "confirm_action",
        lambda *a, **k: calls.append(a) or True,
    )

    dialog = DriveFolderDialog(object(), QThreadPool())
    qtbot.addWidget(dialog)

    dialog._confirm_ingest("folder-1", "Reports", files=[
        {"mimeType": "application/pdf"}, {"mimeType": "application/vnd.google-apps.folder"},
    ])

    assert dialog.selected_folder() == ("folder-1", "Reports")
    assert dialog.result() == QDialog.DialogCode.Accepted
    assert calls[0][-1] == "Sync"


def test_declining_the_confirmation_does_not_accept_the_dialog(qtbot, monkeypatch):
    from jarvis.modules.desktop_app import drive_folder_dialog as drive_folder_dialog_mod

    monkeypatch.setattr(drive_folder_dialog_mod, "confirm_action", lambda *a, **k: False)

    dialog = DriveFolderDialog(object(), QThreadPool())
    qtbot.addWidget(dialog)

    dialog._confirm_ingest("folder-1", "Reports", files=[])

    assert dialog.result() != QDialog.DialogCode.Accepted


def test_select_clicked_runs_the_real_recursive_scan_then_confirms(qtbot, monkeypatch):
    """Not just "_confirm_ingest works in isolation" - confirms
    _on_select_clicked is wired to the real run_daemon_call ->
    drive.list_drive_files_recursive path, the same threading seam
    every other potentially-slow call in this app already uses."""
    from jarvis.modules.desktop_app import drive_folder_dialog as mod

    monkeypatch.setattr(
        mod.drive, "list_drive_files_recursive",
        lambda service, folder_id: [{"mimeType": "application/pdf"}] if folder_id == "folder-1" else [],
    )
    monkeypatch.setattr(mod, "confirm_action", lambda *a, **k: True)

    dialog = DriveFolderDialog(object(), QThreadPool())
    qtbot.addWidget(dialog)
    root = dialog._tree.topLevelItem(0)
    dialog._on_folders_loaded(root, [_folder("folder-1", "Reports")])
    dialog._on_item_clicked(root.child(0), 0)

    dialog._on_select_clicked()

    qtbot.waitUntil(lambda: dialog.result() == QDialog.DialogCode.Accepted, timeout=2000)
    assert dialog.selected_folder() == ("folder-1", "Reports")


def test_a_scan_failure_that_is_not_a_rate_limit_still_offers_to_proceed(qtbot, monkeypatch):
    """The preview is a nice-to-have confirmation, not a precondition
    (module docstring) - a scan failure shouldn't block ingesting the
    folder outright, only the item-count summary in the confirmation."""
    from jarvis.modules.desktop_app import drive_folder_dialog as mod

    monkeypatch.setattr(mod.drive, "is_rate_limited", lambda exc: False)
    monkeypatch.setattr(mod, "confirm_action", lambda *a, **k: True)

    dialog = DriveFolderDialog(object(), QThreadPool())
    qtbot.addWidget(dialog)

    dialog._on_preview_scan_failed("folder-1", "Reports", RuntimeError("network blip"))

    assert dialog.selected_folder() == ("folder-1", "Reports")
    assert dialog.result() == QDialog.DialogCode.Accepted


def test_a_rate_limited_scan_failure_shows_an_error_and_does_not_proceed(qtbot, monkeypatch):
    from jarvis.modules.desktop_app import drive_folder_dialog as mod

    monkeypatch.setattr(mod.drive, "is_rate_limited", lambda exc: True)
    critical_calls = []
    monkeypatch.setattr(mod.QMessageBox, "critical", staticmethod(lambda *a, **k: critical_calls.append(a)))

    def _should_not_be_asked(*a, **k):
        raise AssertionError("confirm_action should not be called for a rate-limited scan failure")

    monkeypatch.setattr(mod, "confirm_action", _should_not_be_asked)

    dialog = DriveFolderDialog(object(), QThreadPool())
    qtbot.addWidget(dialog)

    dialog._on_preview_scan_failed("folder-1", "Reports", RuntimeError("rate limited"))

    assert len(critical_calls) == 1
    assert dialog.result() != QDialog.DialogCode.Accepted
