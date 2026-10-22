"""Guidance for connecting a packaged Jarvis build to Claude Desktop as
an MCP server (`jarvis/cmd/desktop_app.py`'s `--mcp` branch).

**`build_plan()` stays generate-and-copy, read-only - `apply_plan()` is
the one write path, opted into explicitly.** The original design here
was copy-paste only: automated merges of a file this app doesn't own
were judged too risky to do silently (real installs have been seen with
hundreds of unrelated lines of app state alongside `mcpServers`). That
risk is real, but it's a reason to make writing an explicit, confirmed,
backed-up action - not a reason to never offer it. `build_plan()` itself
is unchanged: still purely a read, still never creates or touches the
file. `apply_plan()` is new, separate, and only ever called from the
dialog's own explicit "Add automatically" button after the user
confirms - never automatically, never as a side effect of building the
plan or displaying the dialog.

**Why a fragment (for display) but a full merged dict (for writing).**
Claude Desktop's config can already hold an `mcpServers` key with other
servers in it, or other top-level keys entirely (preferences, window
state, ...) even when `mcpServers` itself is absent - confirmed
directly against a real installed Claude Desktop config on this
machine, which had over 60 lines of unrelated `preferences` state
alongside `mcpServers`. `snippet` (for a human to read and optionally
copy) stays the smallest relevant fragment, unchanged; `full_config`
(for `apply_plan()` to write) is always the *entire* resulting file -
every other key and every other configured MCP server preserved
untouched, with only `mcpServers.jarvis` added or replaced. `None`
exactly when there's nothing safe to auto-write (unsupported platform,
dev mode, or a file that isn't valid JSON to begin with - that last
case stays manual-only, on purpose: this module has no way to know
what "fix" the user actually wants for content it can't parse).
"""

import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

CASE_UNSUPPORTED_PLATFORM = "unsupported_platform"
CASE_DEV_MODE = "dev_mode"
CASE_TRANSLOCATED = "translocated"
CASE_MISSING = "missing"
CASE_UNREADABLE = "unreadable"
CASE_NO_MCP_SERVERS = "no_mcp_servers"
CASE_HAS_MCP_SERVERS = "has_mcp_servers"


@dataclass
class ConnectClaudeDesktopPlan:
    config_path: Optional[Path]
    case: str
    instructions: str
    # None exactly when there's nothing to copy - unsupported platform,
    # or dev mode (no single exe path to point Claude Desktop at).
    snippet: Optional[str]
    # The complete file `apply_plan()` would write - None exactly when
    # there's nothing safe to auto-write (see module docstring).
    full_config: Optional[dict] = None
    # The "jarvis" entry as it currently exists in the file, before any
    # merge - None except in CASE_HAS_MCP_SERVERS. Lets a caller (setup_
    # health.py) tell "already correct" apart from "stale/wrong path"
    # (e.g. the app moved or was reinstalled) without re-reading the
    # file itself.
    existing_jarvis_entry: Optional[dict] = None


def default_config_path() -> Optional[Path]:
    """Claude Desktop's own config file location:
    `%APPDATA%\\Claude\\claude_desktop_config.json` on Windows,
    `~/Library/Application Support/Claude/claude_desktop_config.json` on
    macOS.

    The Windows path is confirmed directly against a real installed
    Claude Desktop on a Windows machine (not just the commonly-
    documented location). The macOS path is Anthropic's own documented
    MCP-setup location for Claude Desktop - not yet verified against a
    real installed instance the way the Windows one was, since this
    project's Mac build hasn't shipped yet; worth confirming once it
    has, the same way the Windows path already was."""
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        return Path(appdata) / "Claude" / "claude_desktop_config.json" if appdata else None
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
    return None


def _is_translocated() -> bool:
    """True under macOS Gatekeeper App Translocation - a quarantined
    .app run without first being opened once via Finder gets relaunched
    from a randomized `.../AppTranslocation/<UUID>/d/...` copy, so
    `sys.executable` is a path that's dead again on the very next
    launch (issue #116). Windows has no equivalent mechanism."""
    return sys.platform == "darwin" and "AppTranslocation" in sys.executable


def frozen_mcp_entry() -> Optional[dict]:
    """The `"jarvis"` mcpServers entry - only when running as the real
    frozen build (`sys.frozen`, the same flag `jarvis/cmd/desktop_app.py`
    dispatches on): `sys.executable` there IS the packaged exe itself.
    Unfrozen, `sys.executable` is a bare interpreter and a working
    dev-mode command needs a venv path plus `PYTHONPATH` that only the
    developer's own setup knows - guessing one risks a snippet that
    looks right but silently isn't, so this returns None instead.

    Same reasoning applies, and the same None, when `_is_translocated()`
    - `sys.executable` is equally unusable there, just for a macOS-
    specific reason instead of a dev-mode one."""
    if not getattr(sys, "frozen", False):
        return None
    if _is_translocated():
        return None
    return {"command": sys.executable, "args": ["--mcp"]}


def build_plan(
    config_path: Optional[Path] = None, entry: Optional[dict] = None
) -> ConnectClaudeDesktopPlan:
    """Read `config_path` (default: `default_config_path()`) read-only
    and decide what to show for `entry` (default: `frozen_mcp_entry()`).
    Both parameters exist for testing - callers normally pass neither."""
    if config_path is None:
        config_path = default_config_path()
    if entry is None:
        entry = frozen_mcp_entry()

    if config_path is None:
        return ConnectClaudeDesktopPlan(
            config_path=None,
            case=CASE_UNSUPPORTED_PLATFORM,
            instructions="Guided setup is only available on Windows right now.",
            snippet=None,
        )

    if entry is None:
        if getattr(sys, "frozen", False) and _is_translocated():
            return ConnectClaudeDesktopPlan(
                config_path=config_path,
                case=CASE_TRANSLOCATED,
                instructions=(
                    "macOS hasn't fully trusted this app yet, so it's running "
                    "from a temporary, randomized location (Gatekeeper \"app "
                    "translocation\") - any path recorded right now would stop "
                    "working the moment you relaunch. Quit Jarvis, then in "
                    "Finder right-click Jarvis in Applications and choose Open "
                    "once (or run `xattr -dr com.apple.quarantine` on it from "
                    "Terminal), relaunch, and try this again."
                ),
                snippet=None,
            )
        return ConnectClaudeDesktopPlan(
            config_path=config_path,
            case=CASE_DEV_MODE,
            instructions=(
                "You're running Jarvis from source, not a packaged build - "
                "there's no single exe path to point Claude Desktop at. "
                "Build the packaged app to get a guided snippet, or configure "
                "this by hand for your dev environment."
            ),
            snippet=None,
        )

    if not config_path.exists():
        full_file = {"mcpServers": {"jarvis": entry}}
        return ConnectClaudeDesktopPlan(
            config_path=config_path,
            case=CASE_MISSING,
            instructions="This file doesn't exist yet. Create it with this content:",
            snippet=json.dumps(full_file, indent=2),
            full_config=full_file,
        )

    try:
        data = json.loads(config_path.read_text())
    except (OSError, json.JSONDecodeError):
        return ConnectClaudeDesktopPlan(
            config_path=config_path,
            case=CASE_UNREADABLE,
            instructions=(
                "This file exists but isn't valid JSON - open it and fix or "
                "back it up before adding a Claude connection by hand."
            ),
            snippet=None,
        )

    if not isinstance(data, dict) or "mcpServers" not in data:
        # A valid-but-non-dict top level (a bare JSON array, a string,
        # ...) has no sensible way to merge a new key into it -
        # full_config stays None (no auto-apply offered) even though a
        # human reading the instructions below can still cope by hand;
        # an automated merge can't.
        full_config = {**data, "mcpServers": {"jarvis": entry}} if isinstance(data, dict) else None
        return ConnectClaudeDesktopPlan(
            config_path=config_path,
            case=CASE_NO_MCP_SERVERS,
            instructions=(
                "This file exists but has no Claude connection section yet. Add this "
                "as a new top-level entry inside the file's outermost { }, "
                "next to whatever's already there (add a comma after the "
                "preceding entry if it doesn't already end in one):"
            ),
            snippet=f'"mcpServers": {json.dumps({"jarvis": entry}, indent=2)}',
            full_config=full_config,
        )

    # Same reasoning as above, one level down: if mcpServers itself
    # isn't a dict (corrupted/hand-edited into something else), there's
    # no safe "preserve the other servers" merge to do - full_config
    # stays None; existing_servers only ever feeds full_config, so this
    # doesn't affect the always-shown instructions/snippet either.
    existing_servers = data["mcpServers"]
    existing_jarvis_entry = existing_servers.get("jarvis") if isinstance(existing_servers, dict) else None
    full_config = (
        {**data, "mcpServers": {**existing_servers, "jarvis": entry}}
        if isinstance(existing_servers, dict) else None
    )
    return ConnectClaudeDesktopPlan(
        config_path=config_path,
        case=CASE_HAS_MCP_SERVERS,
        instructions=(
            "This file already has a Claude connection section. Add this inside it, "
            'next to any other servers already configured there (replace the '
            'existing "jarvis" entry instead, if one is already present):'
        ),
        snippet=f'"jarvis": {json.dumps(entry, indent=2)}',
        full_config=full_config,
        existing_jarvis_entry=existing_jarvis_entry,
    )


def apply_plan(plan: ConnectClaudeDesktopPlan) -> Optional[Path]:
    """Actually write `plan.full_config` to `plan.config_path` - the one
    write path in this module (see module docstring). Only ever called
    from the dialog's own explicit "Add automatically" button, after
    the user has confirmed, never from `build_plan()` or as a side
    effect of displaying anything.

    Backs up whatever's currently at `config_path` to `<name>.bak`
    first (overwriting any previous backup - this is a manual, occasional,
    user-initiated action, not something that runs repeatedly enough for
    a rolling history to matter), then writes the new content to a
    temporary file and `os.replace()`s it into place - atomic on both
    Windows and POSIX, so a crash or a full disk mid-write can never
    leave a half-written config file behind (the same staged-write
    discipline `jarvis_server.internal.graph_db.staged_build` already
    uses for the graph file, applied here for the same reason).

    Returns the backup path, or `None` if there was nothing to back up
    (the file didn't exist yet). Raises `ValueError` if `plan.full_config`
    is `None` - the dialog only ever offers this button when it isn't,
    but calling this directly with an unwritable plan is a programming
    error, not a runtime condition to degrade gracefully for."""
    if plan.full_config is None:
        raise ValueError(f"Nothing safe to auto-write for case={plan.case!r}")

    config_path = plan.config_path
    config_path.parent.mkdir(parents=True, exist_ok=True)

    backup_path: Optional[Path] = None
    if config_path.exists():
        backup_path = config_path.with_name(config_path.name + ".bak")
        backup_path.write_text(config_path.read_text(encoding="utf-8"), encoding="utf-8")

    tmp_path = config_path.with_name(config_path.name + ".tmp")
    tmp_path.write_text(json.dumps(plan.full_config, indent=2), encoding="utf-8")
    os.replace(tmp_path, config_path)

    return backup_path
