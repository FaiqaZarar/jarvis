"""Tests for the collapsible section widget (Figma parity pass, Phase
10) in isolation - not Settings' own wiring, which test_desktop_app_views.py
covers for the Advanced section specifically."""

from PySide6.QtWidgets import QLabel

from jarvis.modules.desktop_app.collapsible_section import CollapsibleSection


def test_collapsed_by_default(qtbot):
    # isHidden(), not isVisible() - this widget is never actually shown
    # on screen in a test, so isVisible() (which depends on the whole
    # ancestor chain being on screen) would report False regardless of
    # this widget's own show()/hide() state. isHidden() reflects that
    # own state directly.
    section = CollapsibleSection("Advanced")
    qtbot.addWidget(section)

    assert section._body.isHidden() is True


def test_expanded_true_at_construction_starts_open(qtbot):
    section = CollapsibleSection("Advanced", expanded=True)
    qtbot.addWidget(section)

    assert section._body.isHidden() is False


def test_clicking_the_header_toggles_the_body(qtbot):
    section = CollapsibleSection("Advanced")
    qtbot.addWidget(section)

    section.toggle()
    assert section._body.isHidden() is False

    section.toggle()
    assert section._body.isHidden() is True


def test_added_widgets_live_in_the_body(qtbot):
    section = CollapsibleSection("Advanced", expanded=True)
    qtbot.addWidget(section)
    label = QLabel("a diagnostic")

    section.add_widget(label)

    assert label.parent() is section._body
