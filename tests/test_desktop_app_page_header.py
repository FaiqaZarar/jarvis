"""Tests for the per-page header bar (page_header.py, Figma parity
pass, Phase 9) in isolation - not any view's own wiring, which
test_desktop_app_views.py covers for Sources/Search specifically."""

from PySide6.QtWidgets import QPushButton

from jarvis.modules.desktop_app.page_header import PageHeader


def test_renders_the_title(qtbot):
    header = PageHeader("Sources")
    qtbot.addWidget(header)

    assert header._title_label.text() == "Sources"


def test_omits_the_action_button_entirely_when_none_given(qtbot):
    header = PageHeader("Search")
    qtbot.addWidget(header)

    assert header.action_button is None
    assert header.findChild(QPushButton) is None


def test_action_button_is_labelled_and_invokes_its_callback(qtbot):
    calls = []
    header = PageHeader("Sources", action=("Add a source", lambda: calls.append(True)))
    qtbot.addWidget(header)

    assert header.action_button.text() == "Add a source"
    assert header.action_button.property("buttonKind") == "primary"
    header.action_button.click()

    assert calls == [True]
