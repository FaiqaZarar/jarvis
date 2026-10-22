"""Tests for the Add-source chooser (DESIGN.md §4.10) in isolation -
not ConnectorsView's wiring, which test_desktop_app_views.py covers."""

from PySide6.QtWidgets import QDialog, QLabel, QPushButton

from jarvis.modules.desktop_app.add_source_dialog import DRIVE, FOLDER, AddSourceDialog


def test_has_folder_and_drive_tiles_and_cancel(qtbot):
    dialog = AddSourceDialog()
    qtbot.addWidget(dialog)

    names = {b.accessibleName() or b.text() for b in dialog.findChildren(QPushButton)}
    assert names == {"Folder", "Google Drive", "Cancel"}
    assert dialog.chosen() is None


def test_each_tile_has_an_icon_and_a_one_line_description(qtbot):
    dialog = AddSourceDialog()
    qtbot.addWidget(dialog)

    for tile in (dialog.folder_tile, dialog.drive_tile):
        icon = tile.findChild(QLabel, "sourceIconTile")
        assert icon is not None and not icon.pixmap().isNull()
        assert tile.findChild(QLabel, "sourceOptionDescription").text()


def test_dialog_is_padded_and_cancel_is_bottom_right(qtbot):
    dialog = AddSourceDialog()
    qtbot.addWidget(dialog)
    dialog.show()
    qtbot.waitExposed(dialog)

    assert dialog.layout().contentsMargins().left() == 24
    cancel = dialog.cancel_button.geometry()
    assert cancel.top() > dialog.drive_tile.geometry().bottom()
    assert cancel.right() >= dialog.drive_tile.geometry().right() - 1


def test_clicking_folder_sets_chosen_and_accepts(qtbot):
    dialog = AddSourceDialog()
    qtbot.addWidget(dialog)

    dialog.folder_tile.click()

    assert dialog.chosen() == FOLDER
    assert dialog.result() == QDialog.DialogCode.Accepted


def test_clicking_google_drive_sets_chosen_and_accepts(qtbot):
    dialog = AddSourceDialog()
    qtbot.addWidget(dialog)

    dialog.drive_tile.click()

    assert dialog.chosen() == DRIVE
    assert dialog.result() == QDialog.DialogCode.Accepted


def test_cancel_rejects_with_no_choice(qtbot):
    dialog = AddSourceDialog()
    qtbot.addWidget(dialog)

    dialog.cancel_button.click()

    assert dialog.result() == QDialog.DialogCode.Rejected
    assert dialog.chosen() is None
