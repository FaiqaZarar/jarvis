"""Shared `.env` loading, plus the packaged desktop app's persisted
embedding-provider settings (docs/desktop-app-DESIGN.md §6/§8.6, §8.8).

**The gap this closes**: `_spawn_daemon()`'s subprocess `cwd` doesn't
resolve to anything meaningful once frozen (confirmed directly during
the real PyInstaller packaging spike), so `load_dotenv()`'s own cwd-based
upward search silently finds nothing on a genuinely fresh, packaged
install - `EMBEDDING_API_KEY` ends up unset with no clear signal why. The
desktop app's first-run settings screen (`jarvis.modules.desktop_app.
settings_dialog`) asks the user for it directly and persists it here
instead, at `<JARVIS_DAEMON_HOME>/.env` - the same OS-standard per-user
app-data directory already used for the db/graph/log files
(`jarvis_server.internal.db.connection`, `jarvis_server.internal.graph_db.graph_db`),
not a location inside the frozen build itself.

**Dev mode is untouched, on purpose.** From source, `load_env()` tries a
real repo-relative `.env` first (`load_dotenv()`'s own upward search) - a
developer with a `.env` already in place keeps working exactly as before.
The packaged location is a second fallback, tried only after, and never
overrides a value the dev-mode search already found (`load_dotenv`'s own
`override=False` default) - so if both exist, dev mode wins.

A frozen build skips the dev-mode search entirely: there `load_dotenv()`
searches upward from the current directory, so launching the installed
app from a terminal inside any project with its own `.env` would quietly
pick up that project's key.

Both `jarvis_server.daemon.server` (the daemon process) and
`jarvis.modules.desktop_app.app`/`main_window` (the GUI process, to
decide whether the first-run screen is needed at all) call `load_env()`
- this is the one place that decides what counts as "a key is
configured," so the two can't drift out of sync.

`load_env()` also applies the bundled Google Drive OAuth client
(`_apply_bundled_google_oauth_client`, docs/desktop-app-drive-connector-
DESIGN.md §4) and a bundled embedding API key
(`_apply_bundled_embedding_key`), when a build has them. Public release
builds bundle no embedding key (`packaging/write_bundled_settings.py`), so
`SettingsDialog`'s first-run gate (`has_embedding_api_key()`) asks each
user for their own.
"""

import os
import sys
from pathlib import Path
from typing import Optional

from dotenv import dotenv_values, load_dotenv, set_key

from jarvis_server.internal import _bundled_credentials

# Matches provider.py's/llm.py's own DEFAULT_API_BASE - a third copy of
# the same literal, consistent with those two already being independent
# copies rather than one shared import (this module is lower-level than
# either and shouldn't need to import an embedding-specific module just
# for a URL string).
DEFAULT_API_BASE = "https://openrouter.ai/api/v1"
# .env.example's own documented default.
DEFAULT_MODEL = "voyageai/voyage-4"

_SAVED_KEYS = ("EMBEDDING_API_KEY", "EMBEDDING_MODEL", "EMBEDDING_API_BASE")

# Shared Google Drive OAuth client - the project lead confirmed bundling
# this shared pair rather than asking every tester to create their own
# Google Cloud project (docs/desktop-app-drive-connector-DESIGN.md §4).
# Google's own documentation for a "Desktop app" OAuth client type
# doesn't treat the secret as confidential the way a server-side "Web
# application" client's would be - an installed app can't keep it truly
# hidden from its own user anyway.
#
# The real values are baked in at *build* time now, not committed here -
# `_bundled_credentials.py` is CI-overwritten right before each
# platform's PyInstaller invocation (build.yml). Per-tester `.env`
# values still take priority (see `_apply_bundled_google_oauth_client`
# below) - that manual path was the original fix for a two-tester group
# and still works as a fallback, it's just no longer the only one.
def _apply_bundled_google_oauth_client() -> None:
    client_id = _bundled_credentials.BUNDLED_GOOGLE_OAUTH_CLIENT_ID
    client_secret = _bundled_credentials.BUNDLED_GOOGLE_OAUTH_CLIENT_SECRET
    if client_id and not os.environ.get("GOOGLE_OAUTH_CLIENT_ID"):
        os.environ["GOOGLE_OAUTH_CLIENT_ID"] = client_id
    if client_secret and not os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET"):
        os.environ["GOOGLE_OAUTH_CLIENT_SECRET"] = client_secret


# Same build-time-baked shape as the Google OAuth client above, for the
# same reason: testers get a working embedding setup with no per-tester
# key of their own to go create - this is a test-phase convenience, not
# something to keep bundling once testers are expected to bring their
# own. A real per-tester `.env` value still takes priority (see below),
# matching the Google client's own fallback-only precedence.
def _apply_bundled_embedding_key() -> None:
    api_key = _bundled_credentials.BUNDLED_EMBEDDING_API_KEY
    if api_key and not os.environ.get("EMBEDDING_API_KEY"):
        os.environ["EMBEDDING_API_KEY"] = api_key


def default_daemon_home() -> Optional[str]:
    """The OS-standard per-user app-data directory a packaged install
    (no `.env`, no repo checkout) should default `JARVIS_DAEMON_HOME` to
    - `%APPDATA%\\Jarvis\\Jarvis` on Windows, `~/Library/Application
    Support/Jarvis` on macOS, and `$XDG_DATA_HOME/Jarvis/Jarvis`
    (`~/.local/share/Jarvis/Jarvis` by default) on Linux. Both `jarvis.modules.desktop_app.app` (the
    GUI) and `jarvis.cmd.desktop_app`'s `--mcp` branch call this, so the
    two ways a packaged daemon can get auto-spawned always agree on
    where `jarvis.db`/the graph file/the daemon discovery file live.

    Windows value confirmed directly against Qt's own
    `QStandardPaths.AppDataLocation` for org="Jarvis", app="Jarvis"
    (`C:/Users/<user>/AppData/Roaming/Jarvis/Jarvis`) on a real machine.
    The macOS value is the standard, documented Apple convention
    (`~/Library/Application Support/<AppName>`) - not verified against a
    real installed instance the same way the Windows one was, since this
    project's Mac build hasn't shipped yet; worth confirming once it
    has. The Linux value is Qt's `AppDataLocation` there, following the
    XDG base-directory spec. Without it the daemon fell back to its own
    install folder, which a .deb install leaves root-owned. No platform
    needs PySide6 importable to compute this."""
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        return str(Path(appdata) / "Jarvis" / "Jarvis") if appdata else None
    if sys.platform == "darwin":
        return str(Path.home() / "Library" / "Application Support" / "Jarvis")
    data_home = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return str(Path(data_home) / "Jarvis" / "Jarvis")


def _packaged_env_path(daemon_home: Optional[Path] = None) -> Optional[Path]:
    if daemon_home is None:
        raw = os.environ.get("JARVIS_DAEMON_HOME")
        daemon_home = Path(raw) if raw else None
    return (daemon_home / ".env") if daemon_home is not None else None


def load_env(daemon_home: Optional[Path] = None) -> None:
    """Dev-mode `.env` first (unchanged), then the packaged fallback at
    `<daemon_home>/.env` (or `$JARVIS_DAEMON_HOME/.env` if `daemon_home`
    isn't given) - see module docstring for why both exist and why the
    order matters. Finally, the bundled Google Drive OAuth client
    (`_apply_bundled_google_oauth_client`) - last, so either `.env`
    source above always wins if it already set those two vars."""
    if not getattr(sys, "frozen", False):
        load_dotenv()
    packaged = _packaged_env_path(daemon_home)
    if packaged is not None:
        load_dotenv(packaged)
    _apply_bundled_google_oauth_client()
    _apply_bundled_embedding_key()


def has_embedding_api_key() -> bool:
    """True once `load_env()` (or an equivalent real `.env`/environment
    setup) has put a real `EMBEDDING_API_KEY` in `os.environ`. Presence-
    only, not a live validation call against OpenRouter - a wrong key
    still fails clearly and loudly on first real use (see
    OpenRouterVoyageEmbedder's own 401/403 handling), which is enough:
    validating it here too would just be the same check done twice, at
    the cost of a network call before the app has even opened."""
    return bool(os.environ.get("EMBEDDING_API_KEY"))


def read_saved_settings(daemon_home: Optional[Path] = None) -> dict:
    """Whatever is currently persisted at `<daemon_home>/.env`, if
    anything - for pre-filling the settings dialog when the user
    revisits it. Never touches `os.environ` (`dotenv_values`, not
    `load_dotenv`) since this is a read for display, not a load."""
    packaged = _packaged_env_path(daemon_home)
    if packaged is None or not packaged.exists():
        return {}
    values = dotenv_values(str(packaged))
    return {k: v for k, v in values.items() if k in _SAVED_KEYS and v}


def telemetry_enabled() -> bool:
    """The Settings "Share usage and diagnostics" toggle's current state.
    On unless switched off - see `telemetry.config.resolve_config`."""
    return os.environ.get("JARVIS_TELEMETRY", "").strip().lower() not in {"0", "false", "off", "no"}


def save_telemetry_enabled(enabled: bool, daemon_home: Optional[Path] = None) -> Path:
    """Persist the toggle to `<daemon_home>/.env`. Both processes read it
    at startup (`load_env`), so it takes full effect on the next launch."""
    packaged = _packaged_env_path(daemon_home)
    if packaged is None:
        raise RuntimeError(
            "Cannot save settings: JARVIS_DAEMON_HOME is not set and no daemon_home was given."
        )
    packaged.parent.mkdir(parents=True, exist_ok=True)
    packaged.touch(exist_ok=True)
    value = "1" if enabled else "0"
    set_key(str(packaged), "JARVIS_TELEMETRY", value)
    os.environ["JARVIS_TELEMETRY"] = value
    return packaged


def save_settings(
    api_key: str,
    model: str,
    api_base: str = "",
    daemon_home: Optional[Path] = None,
) -> Path:
    """Persist to `<daemon_home>/.env` (creating the directory/file if
    needed - a fresh install has neither yet) and apply immediately to
    this process's own `os.environ`, so the change takes effect without
    needing a fresh `load_env()` call. Returns the path written to.

    Uses `dotenv.set_key` rather than hand-writing the file: it updates
    an existing key in place (or appends a new one) and quotes values
    correctly, instead of blindly overwriting the whole file's content
    every time."""
    packaged = _packaged_env_path(daemon_home)
    if packaged is None:
        raise RuntimeError(
            "Cannot save settings: JARVIS_DAEMON_HOME is not set and no daemon_home was given."
        )
    packaged.parent.mkdir(parents=True, exist_ok=True)
    packaged.touch(exist_ok=True)
    # Best-effort: the containing per-user app-data directory is already
    # private on both target platforms, so this is defense-in-depth, not
    # closing a real exposure. A no-op-in-effect on Windows (os.chmod
    # there only toggles the read-only attribute, not real ACLs) but a
    # real restriction on macOS/Linux.
    try:
        os.chmod(packaged, 0o600)
    except OSError:
        pass
    set_key(str(packaged), "EMBEDDING_API_KEY", api_key)
    os.environ["EMBEDDING_API_KEY"] = api_key
    set_key(str(packaged), "EMBEDDING_MODEL", model)
    os.environ["EMBEDDING_MODEL"] = model
    if api_base:
        set_key(str(packaged), "EMBEDDING_API_BASE", api_base)
        os.environ["EMBEDDING_API_BASE"] = api_base
    return packaged
