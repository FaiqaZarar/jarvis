"""Tests for the generic EmptyStateCard (DESIGN.md §4.8) in isolation -
not the view wiring, which test_desktop_app_views.py covers, the same
split every other widget in this app's test suite keeps."""

from PySide6.QtWidgets import QPushButton, QSizePolicy

from jarvis.modules.desktop_app.empty_state import MAX_WIDTH, EmptyStateCard


def test_renders_title_only_when_no_message_or_action(qtbot):
    card = EmptyStateCard("Topics appear after your first sync.")
    qtbot.addWidget(card)

    assert card.findChild(QPushButton) is None


def test_renders_title_and_message(qtbot):
    from PySide6.QtWidgets import QLabel

    card = EmptyStateCard("No files matched.", "Add more sources to search more of your files.")
    qtbot.addWidget(card)

    title, message = card.findChildren(QLabel)
    assert title.objectName() == "emptyStateTitle"
    assert title.text() == "No files matched."
    assert message.objectName() == "emptyStateMessage"
    assert message.text() == "Add more sources to search more of your files."
    assert card.findChild(QPushButton) is None


def test_message_label_absent_entirely_when_message_is_empty(qtbot):
    from PySide6.QtWidgets import QLabel

    card = EmptyStateCard("Topics appear after your first sync.")
    qtbot.addWidget(card)

    labels = card.findChildren(QLabel)
    assert len(labels) == 1
    assert labels[0].objectName() == "emptyStateTitle"


def test_renders_action_button_and_invokes_callback_on_click(qtbot):
    clicked = []
    card = EmptyStateCard(
        "No files matched.", "Add more sources to search more of your files.",
        "Go to Sources", lambda: clicked.append(True),
    )
    qtbot.addWidget(card)

    button = card.findChild(QPushButton)
    assert button.text() == "Go to Sources"

    button.click()

    assert clicked == [True]


def test_no_action_button_when_action_text_given_without_callback(qtbot):
    card = EmptyStateCard("Title", "Message", action_text="Do thing", on_action=None)
    qtbot.addWidget(card)

    assert card.findChild(QPushButton) is None


def test_no_action_button_when_callback_given_without_action_text(qtbot):
    card = EmptyStateCard("Title", "Message", action_text=None, on_action=lambda: None)
    qtbot.addWidget(card)

    assert card.findChild(QPushButton) is None


def test_card_is_compact_and_its_action_is_primary(qtbot):
    card = EmptyStateCard("No files matched.", "Add more sources.", action_text="Go to Sources", on_action=lambda: None)
    qtbot.addWidget(card)

    assert card._frame.maximumWidth() == MAX_WIDTH
    assert card._frame.sizePolicy().verticalPolicy() == QSizePolicy.Policy.Maximum
    assert card.findChild(QPushButton).property("buttonKind") == "primary"


def test_card_sits_centred_at_the_top_at_full_width(qtbot):
    card = EmptyStateCard("No files matched.", "Add more sources to search more of your files.")
    qtbot.addWidget(card)
    card.resize(1000, 600)
    card.show()
    qtbot.waitExposed(card)

    frame = card._frame.geometry()
    assert frame.width() == MAX_WIDTH
    assert abs(frame.left() - (1000 - frame.right() - 1)) <= 1
    assert frame.top() == 0
    assert frame.height() < 300
