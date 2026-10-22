"""Pure tests for setup_health.compute_setup_health - no Qt involved."""

from jarvis.modules.desktop_app import claude_desktop_setup as cds
from jarvis.modules.desktop_app import setup_health


def _plan(case, existing_jarvis_entry=None):
    return cds.ConnectClaudeDesktopPlan(
        config_path=None, case=case, instructions="", snippet=None,
        existing_jarvis_entry=existing_jarvis_entry,
    )


def test_connected_when_existing_entry_matches_frozen_entry(monkeypatch):
    monkeypatch.setattr(cds, "frozen_mcp_entry", lambda: {"command": "x", "args": ["--mcp"]})
    plan = _plan(cds.CASE_HAS_MCP_SERVERS, existing_jarvis_entry={"command": "x", "args": ["--mcp"]})

    health = setup_health.compute_setup_health(
        signed_in=True, has_source=True, claude_plan=plan, claude_dismissed=False,
    )

    assert health.claude_desktop == "connected"
    assert health.all_done is True
    assert health.steps_done == 3


def test_needs_repair_when_existing_entry_does_not_match_frozen_entry(monkeypatch):
    monkeypatch.setattr(cds, "frozen_mcp_entry", lambda: {"command": "x", "args": ["--mcp"]})
    plan = _plan(cds.CASE_HAS_MCP_SERVERS, existing_jarvis_entry={"command": "stale", "args": []})

    health = setup_health.compute_setup_health(
        signed_in=True, has_source=True, claude_plan=plan, claude_dismissed=False,
    )

    assert health.claude_desktop == "needs_repair"
    assert setup_health.claude_desktop_item_state(health) == "needs_attention"


def test_translocated_maps_to_translocated_state():
    plan = _plan(cds.CASE_TRANSLOCATED)

    health = setup_health.compute_setup_health(
        signed_in=True, has_source=True, claude_plan=plan, claude_dismissed=False,
    )

    assert health.claude_desktop == "translocated"
    assert setup_health.claude_desktop_item_state(health) == "needs_attention"


def test_every_other_case_folds_into_not_connected():
    # Explicit scope cut, pinned down per-case: "isn't installed" has no
    # separate detection - see setup_health.py's module docstring.
    for case in (
        cds.CASE_MISSING, cds.CASE_NO_MCP_SERVERS, cds.CASE_UNREADABLE,
        cds.CASE_DEV_MODE, cds.CASE_UNSUPPORTED_PLATFORM,
    ):
        plan = _plan(case)
        health = setup_health.compute_setup_health(
            signed_in=True, has_source=True, claude_plan=plan, claude_dismissed=False,
        )
        assert health.claude_desktop == "not_connected", case


def test_has_mcp_servers_with_no_existing_jarvis_entry_is_not_connected():
    plan = _plan(cds.CASE_HAS_MCP_SERVERS, existing_jarvis_entry=None)

    health = setup_health.compute_setup_health(
        signed_in=True, has_source=True, claude_plan=plan, claude_dismissed=False,
    )

    assert health.claude_desktop == "not_connected"


def test_dismissed_overrides_every_other_case():
    plan = _plan(cds.CASE_HAS_MCP_SERVERS, existing_jarvis_entry={"command": "stale", "args": []})

    health = setup_health.compute_setup_health(
        signed_in=True, has_source=True, claude_plan=plan, claude_dismissed=True,
    )

    assert health.claude_desktop == "dismissed"


def test_dismissed_counts_toward_steps_done_and_all_done():
    plan = _plan(cds.CASE_MISSING)

    health = setup_health.compute_setup_health(
        signed_in=True, has_source=True, claude_plan=plan, claude_dismissed=True,
    )

    assert health.steps_done == 3
    assert health.all_done is True


def test_has_source_none_never_counts_toward_steps_done():
    plan = _plan(cds.CASE_MISSING)

    health = setup_health.compute_setup_health(
        signed_in=True, has_source=None, claude_plan=plan, claude_dismissed=False,
    )

    assert setup_health.source_item_state(health) == "checking"
    assert health.steps_done == 1
    assert health.all_done is False


def test_signed_in_and_source_item_states():
    plan = _plan(cds.CASE_MISSING)

    not_signed_in = setup_health.compute_setup_health(
        signed_in=False, has_source=False, claude_plan=plan, claude_dismissed=False,
    )
    assert setup_health.signed_in_item_state(not_signed_in) == "to_do"
    assert setup_health.source_item_state(not_signed_in) == "to_do"

    signed_in = setup_health.compute_setup_health(
        signed_in=True, has_source=True, claude_plan=plan, claude_dismissed=False,
    )
    assert setup_health.signed_in_item_state(signed_in) == "done"
    assert setup_health.source_item_state(signed_in) == "done"
