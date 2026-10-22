"""Tests for the setup_checklist widgets in isolation (dismiss
persistence, row rendering, action callbacks) - not ConnectorsView's
wiring, which test_desktop_app_views.py covers separately, the same
split test_desktop_app_nav_rail.py already keeps from that file."""

import pytest
from PySide6.QtCore import QSettings, QSize
from PySide6.QtGui import QTextDocumentFragment
from PySide6.QtWidgets import QLabel, QPushButton, QSizePolicy

from jarvis.modules.desktop_app import setup_checklist as sc_mod
from jarvis.modules.desktop_app.setup_checklist import (
    ChecklistItem,
    SetupChecklistCard,
    SetupChecklistStrip,
)


@pytest.fixture
def isolated_settings(tmp_path, monkeypatch):
    ini_path = tmp_path / "test_setup_checklist_settings.ini"
    monkeypatch.setattr(
        sc_mod, "_settings",
        lambda: QSettings(str(ini_path), QSettings.Format.IniFormat),
    )
    return ini_path


def test_dismiss_flag_defaults_to_false(isolated_settings):
    assert sc_mod.is_claude_desktop_dismissed() is False


def test_dismiss_flag_persists_across_reconstruction(isolated_settings):
    sc_mod.set_claude_desktop_dismissed(True)

    assert sc_mod.is_claude_desktop_dismissed() is True


def test_card_renders_all_three_rows_and_hides_the_dismissed_one(qtbot):
    items = [
        ChecklistItem("Signed in", "done"),
        ChecklistItem("Add your first source", "to_do"),
        ChecklistItem("Connect Claude Desktop (optional)", "dismissed"),
    ]
    card = SetupChecklistCard()
    qtbot.addWidget(card)

    card.update_items(items)

    assert len(card._rows) == 3
    assert card._rows[0].isHidden() is False
    assert card._rows[1].isHidden() is False
    assert card._rows[2].isHidden() is True


def test_strip_shows_n_of_3_done_and_only_outstanding_rows(qtbot):
    items = [
        ChecklistItem("Signed in", "done"),
        ChecklistItem("Add your first source", "done"),
        ChecklistItem("Connect Claude Desktop (optional)", "needs_attention"),
    ]
    strip = SetupChecklistStrip()
    qtbot.addWidget(strip)

    strip.update_items(items, steps_done=2, total_steps=3)

    assert len(strip._rows) == 1
    assert "2 of 3 done." in strip._rows[0]._label.text()


def test_strip_leads_with_the_full_catalogue_sentence_for_the_next_step(qtbot):
    items = [
        ChecklistItem("Signed in", "done"),
        ChecklistItem("Add your first source", "done"),
        ChecklistItem(
            "Connect Claude Desktop (optional).", "to_do",
            primary_action=("Connect", lambda: None),
            strip_text="Connect Claude Desktop so Claude can use your files.",
        ),
    ]
    strip = SetupChecklistStrip()
    qtbot.addWidget(strip)

    strip.update_items(items, steps_done=2, total_steps=3)

    label = strip._rows[0]._label
    plain = QTextDocumentFragment.fromHtml(label.text()).toPlainText()
    assert plain == "Setup. 2 of 3 done. Connect Claude Desktop so Claude can use your files."
    assert "font-weight:600'>Setup.</span>" in label.text()
    assert strip._rows[0].findChild(QPushButton).text() == "Connect"


def test_every_item_state_gets_a_16px_icon_not_a_text_glyph(qtbot):
    card = SetupChecklistCard()
    qtbot.addWidget(card)

    card.update_items([
        ChecklistItem("Signed in", "done"),
        ChecklistItem("Add your first source", "to_do"),
        ChecklistItem("The Claude connection needs repair.", "needs_attention"),
        ChecklistItem("Add your first source", "checking"),
    ])

    for row in card._rows:
        assert row._icon.pixmap() is not None and not row._icon.pixmap().isNull()
        assert row._icon.size() == QSize(16, 16)
        assert row._label.text() in (
            "Signed in", "Add your first source", "The Claude connection needs repair.",
        )


def test_row_label_wraps_so_buttons_never_force_the_row_wider(qtbot):
    item = ChecklistItem(
        "A long sentence about connecting Claude Desktop so Claude can use your files.", "to_do",
        primary_action=("Connect", lambda: None),
        secondary_action=("I don't use Claude Desktop", lambda: None),
    )
    strip = SetupChecklistStrip()
    qtbot.addWidget(strip)
    strip.update_items([item], steps_done=2, total_steps=3)

    label = strip._rows[0]._label
    assert label.wordWrap()
    assert label.sizePolicy().horizontalPolicy() == QSizePolicy.Policy.Ignored


def test_emphasized_item_makes_its_main_action_primary_and_places_it_last(qtbot):
    item = ChecklistItem(
        "Add your first source", "to_do",
        primary_action=("Add a source", lambda: None),
        secondary_action=("Not now", lambda: None),
        emphasized=True,
    )
    card = SetupChecklistCard()
    qtbot.addWidget(card)
    card.update_items([item])

    buttons = card._rows[0].findChildren(QPushButton)
    assert [b.text() for b in buttons] == ["Not now", "Add a source"]
    assert buttons[1].property("buttonKind") == "primary"


def test_card_is_never_taller_than_its_content_and_has_a_capped_width(qtbot):
    card = SetupChecklistCard()
    qtbot.addWidget(card)

    assert card.maximumWidth() == sc_mod.CARD_MAX_WIDTH
    assert card.sizePolicy().verticalPolicy() == QSizePolicy.Policy.Maximum


def test_row_renders_an_optional_message_line(qtbot):
    item = ChecklistItem(
        "Add your first source", "to_do",
        message="Point Jarvis at a folder or Google Drive. It reads the files once, then keeps them current.",
    )
    card = SetupChecklistCard()
    qtbot.addWidget(card)

    card.update_items([item])

    message_label = card._rows[0].findChild(QLabel, "setupChecklistRowMessage")
    assert message_label is not None
    assert message_label.text().startswith("Point Jarvis at a folder")


def test_row_omits_the_message_line_when_none_given(qtbot):
    card = SetupChecklistCard()
    qtbot.addWidget(card)

    card.update_items([ChecklistItem("Signed in", "done")])

    assert card._rows[0].findChild(QLabel, "setupChecklistRowMessage") is None


def test_row_primary_action_invokes_its_callback(qtbot):
    clicked = []
    item = ChecklistItem("Add your first source", "to_do", primary_action=("Add a source", lambda: clicked.append(True)))
    card = SetupChecklistCard()
    qtbot.addWidget(card)
    card.update_items([item])

    card._rows[0].findChild(QPushButton).click()

    assert clicked == [True]


def test_row_secondary_action_invokes_its_callback_as_a_quiet_button(qtbot):
    clicked = []
    item = ChecklistItem(
        "Add your first source", "to_do",
        primary_action=("Add a source", lambda: None),
        secondary_action=("Not now", lambda: clicked.append(True)),
    )
    card = SetupChecklistCard()
    qtbot.addWidget(card)
    card.update_items([item])

    buttons = card._rows[0].findChildren(QPushButton)
    assert len(buttons) == 2
    secondary = next(b for b in buttons if b.text() == "Not now")
    assert secondary.property("buttonKind") == "quiet"

    secondary.click()

    assert clicked == [True]


def test_update_items_replaces_previous_rows_not_accumulates(qtbot):
    card = SetupChecklistCard()
    qtbot.addWidget(card)

    card.update_items([ChecklistItem("A", "to_do")])
    card.update_items([ChecklistItem("A", "to_do"), ChecklistItem("B", "done")])

    assert len(card._rows) == 2


def test_card_rows_keep_the_list_row_height_and_the_card_its_content_height(qtbot):
    """The card is never taller than its content (the Sources page leaves
    the spare height below it), and rows keep DESIGN.md's 48 px list-row
    height."""
    from jarvis.modules.desktop_app import theme

    card = SetupChecklistCard()
    card.setStyleSheet(theme.build_stylesheet())
    qtbot.addWidget(card)
    card.update_items([
        ChecklistItem("Signed in", "done"),
        ChecklistItem("Add your first source", "to_do"),
        ChecklistItem("Connect Claude Desktop (optional).", "to_do"),
    ])
    card.resize(640, card.sizeHint().height())
    card.show()
    qtbot.waitExposed(card)

    heading = card.findChild(QLabel, "setupChecklistHeading")
    rows = [row for row in card.findChildren(sc_mod._ChecklistRow) if row.isVisible()]
    assert heading.geometry().top() < 60
    assert all(row.height() >= theme.LIST_ROW_MIN_HEIGHT for row in rows)
    assert card.sizePolicy().verticalPolicy() == QSizePolicy.Policy.Maximum


def test_strip_never_shows_a_primary_button(qtbot):
    item = ChecklistItem(
        "Add your first source", "to_do", primary_action=("Add a source", lambda: None), emphasized=True,
    )
    strip = SetupChecklistStrip()
    qtbot.addWidget(strip)

    strip.update_items([item], steps_done=1, total_steps=3)

    assert strip._rows[0].findChild(QPushButton).property("buttonKind") in (None, "")
