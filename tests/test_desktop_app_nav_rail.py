"""Expand/collapse, labels, and persisted state for NavRail (DESIGN.md
§4.1) - main_window.py's own geometry-persistence tests
(test_desktop_app_views.py) are the pattern this mirrors, against
nav_rail's own `_settings` seam instead of main_window's."""

import pytest
from PySide6.QtCore import QSettings

from jarvis.modules.desktop_app.views import nav_rail as nav_rail_mod
from jarvis.modules.desktop_app.views.nav_rail import NAV_ITEMS, NavRail


@pytest.fixture
def isolated_settings(tmp_path, monkeypatch):
    ini_path = tmp_path / "test_nav_rail_settings.ini"
    monkeypatch.setattr(
        nav_rail_mod, "_settings",
        lambda: QSettings(str(ini_path), QSettings.Format.IniFormat),
    )
    return ini_path


def test_nav_items_order_and_labels():
    assert [label for label, _svg in NAV_ITEMS] == [
        "Sources", "Search", "Topics", "Activity", "Settings",
    ]


def test_nav_buttons_have_matching_label_tooltip_and_accessible_name(qtbot, isolated_settings):
    rail = NavRail()
    qtbot.addWidget(rail)

    for button, (label, _svg) in zip(rail._nav_buttons, NAV_ITEMS):
        assert button.text() == label
        assert button.toolTip() == label
        assert button.accessibleName() == label


def test_settings_and_collapse_are_the_last_two_widgets_in_layout_order(qtbot, isolated_settings):
    rail = NavRail()
    qtbot.addWidget(rail)

    layout = rail.layout()
    widgets = [layout.itemAt(i).widget() for i in range(layout.count()) if layout.itemAt(i).widget() is not None]

    assert widgets[-2] is rail._nav_buttons[-1]  # Settings
    assert widgets[-1] is rail._collapse_button


def test_default_width_is_expanded_when_nothing_was_persisted_yet(qtbot, isolated_settings):
    rail = NavRail()
    qtbot.addWidget(rail)

    assert rail.width() == nav_rail_mod._EXPANDED_WIDTH
    assert rail._collapsed is False


def test_collapsing_shrinks_width_and_persists_the_choice(qtbot, isolated_settings):
    rail = NavRail()
    qtbot.addWidget(rail)

    rail._collapse_button.click()

    assert rail.width() == nav_rail_mod._COLLAPSED_WIDTH
    assert rail._collapsed is True
    assert nav_rail_mod._settings().value(nav_rail_mod._SETTINGS_KEY, False, type=bool) is True


def test_a_previously_collapsed_choice_survives_reconstruction(qtbot, isolated_settings):
    first = NavRail()
    qtbot.addWidget(first)
    first._collapse_button.click()

    second = NavRail()
    qtbot.addWidget(second)

    assert second.width() == nav_rail_mod._COLLAPSED_WIDTH
    assert second._collapsed is True


def test_expanding_back_restores_width_and_persists_false(qtbot, isolated_settings):
    rail = NavRail()
    qtbot.addWidget(rail)
    rail._collapse_button.click()  # collapse
    rail._collapse_button.click()  # expand again

    assert rail.width() == nav_rail_mod._EXPANDED_WIDTH
    assert rail._collapsed is False
    assert nav_rail_mod._settings().value(nav_rail_mod._SETTINGS_KEY, False, type=bool) is False


def test_collapse_button_label_names_the_action_it_performs(qtbot, isolated_settings):
    rail = NavRail()
    qtbot.addWidget(rail)

    assert rail._collapse_button.text() == "Collapse"

    rail._collapse_button.click()

    assert rail._collapse_button.text() == "Expand"


def test_selected_buttons_icon_swaps_between_default_and_selected(qtbot, isolated_settings):
    rail = NavRail()
    qtbot.addWidget(rail)

    first, second = rail._nav_buttons[0], rail._nav_buttons[1]
    assert first.icon().cacheKey() == first._icon_selected.cacheKey()
    assert second.icon().cacheKey() == second._icon_default.cacheKey()

    second.click()

    assert first.icon().cacheKey() == first._icon_default.cacheKey()
    assert second.icon().cacheKey() == second._icon_selected.cacheKey()


def test_page_selected_still_emits_the_clicked_buttons_index(qtbot, isolated_settings):
    rail = NavRail()
    qtbot.addWidget(rail)

    selected = []
    rail.page_selected.connect(selected.append)

    rail._nav_buttons[2].click()  # Topics

    assert selected == [2]


# ---------------------------------------------------------------------
# Sources tag (DESIGN.md §4.1, Phase 4) - Syncing/Attention indicator.
# ---------------------------------------------------------------------

def test_sources_tag_hidden_by_default(qtbot, isolated_settings):
    rail = NavRail()
    qtbot.addWidget(rail)

    assert rail._sources_tag.isHidden() is True


def test_set_syncing_shows_the_syncing_tag(qtbot, isolated_settings):
    rail = NavRail()
    qtbot.addWidget(rail)

    rail.set_syncing(True)

    assert rail._sources_tag.isHidden() is False
    assert rail._sources_tag.text() == "Syncing"
    assert rail._sources_tag.property("tagKind") == "syncing"


def test_set_needs_attention_shows_the_attention_tag(qtbot, isolated_settings):
    rail = NavRail()
    qtbot.addWidget(rail)

    rail.set_needs_attention(True)

    assert rail._sources_tag.isHidden() is False
    assert rail._sources_tag.text() == "Attention"
    assert rail._sources_tag.property("tagKind") == "attention"


def test_syncing_takes_priority_over_attention_when_both_are_true(qtbot, isolated_settings):
    rail = NavRail()
    qtbot.addWidget(rail)

    rail.set_needs_attention(True)
    rail.set_syncing(True)

    assert rail._sources_tag.text() == "Syncing"


def test_attention_reappears_once_syncing_ends(qtbot, isolated_settings):
    rail = NavRail()
    qtbot.addWidget(rail)

    rail.set_needs_attention(True)
    rail.set_syncing(True)
    rail.set_syncing(False)

    assert rail._sources_tag.text() == "Attention"


def test_both_false_hides_the_tag_again(qtbot, isolated_settings):
    rail = NavRail()
    qtbot.addWidget(rail)

    rail.set_syncing(True)
    rail.set_syncing(False)

    assert rail._sources_tag.isHidden() is True


def test_collapsing_blanks_the_tag_text_but_keeps_its_kind_and_visibility(qtbot, isolated_settings):
    rail = NavRail()
    qtbot.addWidget(rail)
    rail.set_syncing(True)

    rail._collapse_button.click()

    assert rail._sources_tag.text() == ""
    assert rail._sources_tag.property("tagKind") == "syncing"
    assert rail._sources_tag.property("collapsed") is True
    assert rail._sources_tag.isHidden() is False

    rail._collapse_button.click()  # expand again

    assert rail._sources_tag.text() == "Syncing"


def test_the_collapse_arrow_points_the_way_the_rail_will_move(qtbot, isolated_settings, monkeypatch):
    drawn = []
    real_icon_from_svg = nav_rail_mod.icon_from_svg
    monkeypatch.setattr(
        nav_rail_mod, "icon_from_svg", lambda svg, color: drawn.append(svg) or real_icon_from_svg(svg, color),
    )
    rail = NavRail()
    qtbot.addWidget(rail)
    assert drawn[-1] == nav_rail_mod._COLLAPSE_SVG

    rail._collapse_button.click()

    assert drawn[-1] == nav_rail_mod._EXPAND_SVG


@pytest.mark.parametrize("collapse", [False, True])
def test_sources_tag_sits_inside_the_sources_button(qtbot, isolated_settings, collapse):
    # As a layout sibling it pushed the icon out of the collapsed rail and
    # was cut off at the expanded rail's edge.
    rail = NavRail()
    qtbot.addWidget(rail)
    rail.show()
    if collapse:
        rail._collapse_button.click()
    rail.set_syncing(True)
    qtbot.wait(10)

    sources = rail._nav_buttons[0]
    assert rail._sources_tag.parentWidget() is sources
    assert sources.rect().contains(rail._sources_tag.geometry())
