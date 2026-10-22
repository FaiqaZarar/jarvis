"""Claude Desktop connection guidance (jarvis.modules.desktop_app.
claude_desktop_setup) - path detection, entry generation, and the
three-way (plus unreadable/dev-mode/non-Windows) fork over what's
already in Claude Desktop's config file. Every generated snippet is
round-tripped through json.loads here, not just visually inspected."""

import json
import sys

import pytest

from jarvis.modules.desktop_app import claude_desktop_setup as cds

# ---------------------------------------------------------------------
# default_config_path()
# ---------------------------------------------------------------------

def test_default_config_path_on_windows(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("APPDATA", r"C:\Users\someone\AppData\Roaming")

    path = cds.default_config_path()

    assert str(path) == r"C:\Users\someone\AppData\Roaming\Claude\claude_desktop_config.json"


def test_default_config_path_none_when_appdata_unset(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.delenv("APPDATA", raising=False)

    assert cds.default_config_path() is None


def test_default_config_path_on_macos(monkeypatch):
    # Compared as Path objects, not stringified - on a Windows dev
    # machine, pathlib always renders with backslashes regardless of
    # what sys.platform is monkeypatched to (that's decided by the real
    # OS at interpreter startup, not reconfigurable), so a hand-written
    # forward-slash string would fail here for a reason that has nothing
    # to do with the source code being wrong.
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(cds.Path, "home", lambda: cds.Path("/Users/someone"))

    path = cds.default_config_path()

    assert path == cds.Path("/Users/someone") / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"


def test_default_config_path_none_on_unsupported_platform(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")

    assert cds.default_config_path() is None


# ---------------------------------------------------------------------
# frozen_mcp_entry()
# ---------------------------------------------------------------------

def test_frozen_mcp_entry_none_when_not_frozen(monkeypatch):
    monkeypatch.delattr(sys, "frozen", raising=False)

    assert cds.frozen_mcp_entry() is None


def test_frozen_mcp_entry_uses_sys_executable_and_mcp_flag_when_frozen(monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", r"C:\Program Files\Jarvis\jarvis_desktop.exe")

    entry = cds.frozen_mcp_entry()

    assert entry == {"command": r"C:\Program Files\Jarvis\jarvis_desktop.exe", "args": ["--mcp"]}


def test_frozen_mcp_entry_none_when_translocated(monkeypatch):
    """A translocated launch's sys.executable is a randomized per-launch
    temp path - dead again on the very next launch (issue #116). Same
    "don't guess a path that looks right but isn't" call as dev mode."""
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(
        sys, "executable",
        "/private/var/folders/zz/abc123/T/AppTranslocation/9F2E/d/Jarvis.app/Contents/MacOS/jarvis_desktop",
    )

    assert cds.frozen_mcp_entry() is None


def test_frozen_mcp_entry_not_translocated_on_a_normal_macos_launch(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", "/Applications/Jarvis.app/Contents/MacOS/jarvis_desktop")

    entry = cds.frozen_mcp_entry()

    assert entry == {
        "command": "/Applications/Jarvis.app/Contents/MacOS/jarvis_desktop", "args": ["--mcp"],
    }


# ---------------------------------------------------------------------
# build_plan() - the fork over config-file state
# ---------------------------------------------------------------------

_ENTRY = {"command": r"C:\Program Files\Jarvis\jarvis_desktop.exe", "args": ["--mcp"]}


def test_build_plan_unsupported_platform_when_no_config_path(monkeypatch):
    # config_path=None means "compute the real default" (matching
    # env_config.py's daemon_home=None convention) - to actually exercise
    # the no-path-available case, the default itself must return None,
    # the way it really does on a non-Windows platform.
    monkeypatch.setattr(cds, "default_config_path", lambda: None)

    plan = cds.build_plan(entry=_ENTRY)

    assert plan.case == cds.CASE_UNSUPPORTED_PLATFORM
    assert plan.snippet is None


def test_build_plan_dev_mode_when_no_entry(tmp_path):
    plan = cds.build_plan(config_path=tmp_path / "claude_desktop_config.json", entry=None)

    assert plan.case == cds.CASE_DEV_MODE
    assert plan.snippet is None
    assert plan.config_path == tmp_path / "claude_desktop_config.json"  # still shown


def test_build_plan_translocated_when_frozen_but_no_entry(monkeypatch, tmp_path):
    """A translocated launch must read as its own distinct case, not as
    plain dev mode - the fix (quit, Open once from Finder / clear
    quarantine) is nothing like "build the packaged app" (issue #116)."""
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(
        sys, "executable",
        "/private/var/folders/zz/abc123/T/AppTranslocation/9F2E/d/Jarvis.app/Contents/MacOS/jarvis_desktop",
    )

    plan = cds.build_plan(config_path=tmp_path / "claude_desktop_config.json", entry=None)

    assert plan.case == cds.CASE_TRANSLOCATED
    assert plan.snippet is None
    assert plan.full_config is None
    assert "translocat" in plan.instructions.lower()


def test_build_plan_missing_file_shows_a_complete_minimal_file(tmp_path):
    path = tmp_path / "claude_desktop_config.json"
    assert not path.exists()

    plan = cds.build_plan(config_path=path, entry=_ENTRY)

    assert plan.case == cds.CASE_MISSING
    parsed = json.loads(plan.snippet)  # real round-trip, not visual inspection
    assert parsed == {"mcpServers": {"jarvis": _ENTRY}}


def test_build_plan_unreadable_file_is_not_valid_json(tmp_path):
    path = tmp_path / "claude_desktop_config.json"
    path.write_text("{ not valid json ][")

    plan = cds.build_plan(config_path=path, entry=_ENTRY)

    assert plan.case == cds.CASE_UNREADABLE
    assert plan.snippet is None


def test_build_plan_existing_file_without_mcp_servers_key_shows_fragment_only(tmp_path):
    path = tmp_path / "claude_desktop_config.json"
    path.write_text(json.dumps({"coworkUserFilesPath": "C:\\Users\\someone\\Claude"}))

    plan = cds.build_plan(config_path=path, entry=_ENTRY)

    assert plan.case == cds.CASE_NO_MCP_SERVERS
    # The fragment is not itself a standalone JSON document - it's meant
    # to be merged inside the file's existing outer {}. Validate it the
    # way it will actually be used: wrapped in the outer braces it's
    # missing.
    parsed = json.loads("{" + plan.snippet + "}")
    assert parsed == {"mcpServers": {"jarvis": _ENTRY}}
    # And the original file's other content must not appear anywhere in
    # what we're telling the user to paste - we never touch it.
    assert "coworkUserFilesPath" not in plan.snippet


def test_build_plan_existing_file_with_mcp_servers_shows_entry_only(tmp_path):
    path = tmp_path / "claude_desktop_config.json"
    path.write_text(json.dumps({
        "mcpServers": {"other-server": {"command": "something", "args": []}},
        "coworkUserFilesPath": "C:\\Users\\someone\\Claude",
    }))

    plan = cds.build_plan(config_path=path, entry=_ENTRY)

    assert plan.case == cds.CASE_HAS_MCP_SERVERS
    # Likewise: this fragment slots inside the existing "mcpServers": {}.
    parsed = json.loads("{\"mcpServers\": {" + plan.snippet + "}}")
    assert parsed == {"mcpServers": {"jarvis": _ENTRY}}
    assert "other-server" not in plan.snippet
    assert "coworkUserFilesPath" not in plan.snippet


def test_build_plan_existing_jarvis_entry_is_not_special_cased(tmp_path):
    """A real observed case on this machine: an old dev-mode "jarvis"
    entry already present. build_plan doesn't try to detect or merge
    with it - it always shows the fresh entry plus instructions telling
    the user to replace whatever's there, never touching the file."""
    path = tmp_path / "claude_desktop_config.json"
    path.write_text(json.dumps({
        "mcpServers": {"jarvis": {"command": "old-python.exe", "args": ["-m", "old.module"]}},
    }))

    plan = cds.build_plan(config_path=path, entry=_ENTRY)

    assert plan.case == cds.CASE_HAS_MCP_SERVERS
    assert "replace" in plan.instructions.lower()
    parsed = json.loads("{\"mcpServers\": {" + plan.snippet + "}}")
    assert parsed == {"mcpServers": {"jarvis": _ENTRY}}


# ---------------------------------------------------------------------
# full_config - the complete file apply_plan() would write. Snippet
# stays the minimal fragment for display; full_config is always the
# whole merged result, or None when nothing is safe to auto-write.
# ---------------------------------------------------------------------

def test_full_config_missing_file_case_matches_the_snippet(tmp_path):
    plan = cds.build_plan(config_path=tmp_path / "claude_desktop_config.json", entry=_ENTRY)

    assert plan.full_config == {"mcpServers": {"jarvis": _ENTRY}}
    assert plan.full_config == json.loads(plan.snippet)


def test_full_config_no_mcp_servers_case_preserves_other_top_level_keys(tmp_path):
    path = tmp_path / "claude_desktop_config.json"
    path.write_text(json.dumps({"coworkUserFilesPath": "C:\\Users\\someone\\Claude"}))

    plan = cds.build_plan(config_path=path, entry=_ENTRY)

    assert plan.full_config == {
        "coworkUserFilesPath": "C:\\Users\\someone\\Claude",
        "mcpServers": {"jarvis": _ENTRY},
    }


def test_full_config_has_mcp_servers_case_preserves_other_servers_and_keys(tmp_path):
    path = tmp_path / "claude_desktop_config.json"
    path.write_text(json.dumps({
        "mcpServers": {"other-server": {"command": "something", "args": []}},
        "coworkUserFilesPath": "C:\\Users\\someone\\Claude",
    }))

    plan = cds.build_plan(config_path=path, entry=_ENTRY)

    assert plan.full_config == {
        "mcpServers": {
            "other-server": {"command": "something", "args": []},
            "jarvis": _ENTRY,
        },
        "coworkUserFilesPath": "C:\\Users\\someone\\Claude",
    }


def test_full_config_replaces_an_existing_jarvis_entry(tmp_path):
    path = tmp_path / "claude_desktop_config.json"
    path.write_text(json.dumps({
        "mcpServers": {"jarvis": {"command": "old-python.exe", "args": ["-m", "old.module"]}},
    }))

    plan = cds.build_plan(config_path=path, entry=_ENTRY)

    assert plan.full_config == {"mcpServers": {"jarvis": _ENTRY}}


def test_full_config_is_none_when_unreadable(tmp_path):
    path = tmp_path / "claude_desktop_config.json"
    path.write_text("{ not valid json ][")

    plan = cds.build_plan(config_path=path, entry=_ENTRY)

    assert plan.full_config is None


def test_full_config_is_none_when_dev_mode(tmp_path):
    plan = cds.build_plan(config_path=tmp_path / "claude_desktop_config.json", entry=None)

    assert plan.full_config is None


def test_full_config_is_none_when_unsupported_platform(monkeypatch):
    monkeypatch.setattr(cds, "default_config_path", lambda: None)

    plan = cds.build_plan(entry=_ENTRY)

    assert plan.full_config is None


def test_full_config_is_none_when_top_level_json_is_not_an_object(tmp_path):
    """A real, if unusual, edge case: valid JSON that isn't a dict at
    all (a bare array). No sensible key to merge mcpServers into -
    auto-apply must not be offered, even though the case is still
    classified as CASE_NO_MCP_SERVERS and a human can still read the
    instructions."""
    path = tmp_path / "claude_desktop_config.json"
    path.write_text(json.dumps([1, 2, 3]))

    plan = cds.build_plan(config_path=path, entry=_ENTRY)

    assert plan.case == cds.CASE_NO_MCP_SERVERS
    assert plan.full_config is None


def test_full_config_is_none_when_mcp_servers_value_is_not_an_object(tmp_path):
    """Same reasoning one level down: mcpServers present but corrupted
    into something other than a dict - no safe "preserve the other
    servers" merge exists."""
    path = tmp_path / "claude_desktop_config.json"
    path.write_text(json.dumps({"mcpServers": ["not", "a", "dict"]}))

    plan = cds.build_plan(config_path=path, entry=_ENTRY)

    assert plan.case == cds.CASE_HAS_MCP_SERVERS
    assert plan.full_config is None


# ---------------------------------------------------------------------
# apply_plan() - the one write path
# ---------------------------------------------------------------------

def test_apply_plan_creates_a_missing_file_with_no_backup(tmp_path):
    path = tmp_path / "claude_desktop_config.json"
    plan = cds.build_plan(config_path=path, entry=_ENTRY)

    backup = cds.apply_plan(plan)

    assert backup is None
    assert json.loads(path.read_text()) == {"mcpServers": {"jarvis": _ENTRY}}


def test_apply_plan_creates_the_parent_directory_if_missing(tmp_path):
    path = tmp_path / "not-yet-created" / "claude_desktop_config.json"
    plan = cds.build_plan(config_path=path, entry=_ENTRY)

    cds.apply_plan(plan)

    assert path.exists()


def test_apply_plan_backs_up_and_preserves_other_content(tmp_path):
    path = tmp_path / "claude_desktop_config.json"
    original = json.dumps({
        "mcpServers": {"other-server": {"command": "something", "args": []}},
        "coworkUserFilesPath": "C:\\Users\\someone\\Claude",
    })
    path.write_text(original)
    plan = cds.build_plan(config_path=path, entry=_ENTRY)

    backup = cds.apply_plan(plan)

    assert backup == path.with_name(path.name + ".bak")
    assert backup.read_text() == original
    written = json.loads(path.read_text())
    assert written["mcpServers"]["other-server"] == {"command": "something", "args": []}
    assert written["mcpServers"]["jarvis"] == _ENTRY
    assert written["coworkUserFilesPath"] == "C:\\Users\\someone\\Claude"


def test_apply_plan_overwrites_a_previous_backup_rather_than_accumulating(tmp_path):
    path = tmp_path / "claude_desktop_config.json"
    path.write_text(json.dumps({"mcpServers": {}}))
    backup_path = path.with_name(path.name + ".bak")
    backup_path.write_text("stale backup from a previous run")

    plan = cds.build_plan(config_path=path, entry=_ENTRY)
    cds.apply_plan(plan)

    assert backup_path.read_text() == json.dumps({"mcpServers": {}})


def test_apply_plan_leaves_no_leftover_tmp_file(tmp_path):
    path = tmp_path / "claude_desktop_config.json"
    plan = cds.build_plan(config_path=path, entry=_ENTRY)

    cds.apply_plan(plan)

    assert not path.with_name(path.name + ".tmp").exists()


def test_apply_plan_raises_when_full_config_is_none(tmp_path):
    path = tmp_path / "claude_desktop_config.json"
    path.write_text("{ not valid json ][")
    plan = cds.build_plan(config_path=path, entry=_ENTRY)
    assert plan.full_config is None

    with pytest.raises(ValueError):
        cds.apply_plan(plan)

    assert path.read_text() == "{ not valid json ]["  # untouched


def test_build_plan_never_writes_to_the_file(tmp_path):
    """Regression guard for the hard constraint of this whole feature:
    build_plan only ever reads. Covers every case that takes a real
    path, including MISSING (which must not create the file either)."""
    missing_path = tmp_path / "missing" / "claude_desktop_config.json"
    cds.build_plan(config_path=missing_path, entry=_ENTRY)
    assert not missing_path.exists()
    assert not missing_path.parent.exists()  # not even the folder

    existing_path = tmp_path / "claude_desktop_config.json"
    original = json.dumps({"mcpServers": {"x": {}}})
    existing_path.write_text(original)
    cds.build_plan(config_path=existing_path, entry=_ENTRY)
    assert existing_path.read_text() == original
