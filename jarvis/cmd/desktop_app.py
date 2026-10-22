"""Entrypoint for the desktop app - `python -m jarvis.cmd.desktop_app` in
dev, and the packaged app's own executable once frozen (PyInstaller).

**Single frozen executable, argv-dispatched** (docs/desktop-app-DESIGN.md
§4, resolved: option 1). The same packaged binary runs as the GUI, the
daemon, or the MCP stdio server, decided by a `--daemon`/`--mcp` argv
flag - not separate binaries to build, sign, and notarize. `jarvis.
modules.daemon.client._spawn_daemon()`'s frozen branch spawns exactly
`[sys.executable, "--daemon"]` - which the dispatch below catches and
routes to the real daemon entrypoint (`jarvis_server.daemon.server.
main()`, the same thing `jarvis/cmd/daemon.py`'s unfrozen `-m
jarvis.cmd.daemon` path runs - nothing about the daemon's own logic
changes for this). `--mcp` routes to `jarvis.cmd.mcp_server.main()`
unchanged - an MCP client (e.g. Claude Desktop) that would otherwise
spawn `python -m jarvis.cmd.mcp_server` points at the frozen exe with
`--mcp` instead.

Unlike `--daemon` (only ever reached today via the GUI spawning it, or a
developer's own shell with a `.env`/repo checkout in play), `--mcp` and
the GUI are both entrypoints that can auto-spawn the daemon with nothing
else having run first. Both branches below call
`_ensure_daemon_home_default()` - which sets `JARVIS_DAEMON_HOME` to
`env_config.default_daemon_home()` - as the *first* thing they do, before
either branch's own deferred import. This matters in a real frozen
build for two separate, confirmed reasons:

1. Without `JARVIS_DAEMON_HOME` set at all, `jarvis.modules.daemon.client`
   and `jarvis_server.daemon.server` each fall back to their own
   independent `Path(__file__).resolve().parents[N]` guess at the repo
   root, and those two guesses land in different directories once frozen
   (verified only to agree in an unfrozen checkout), so the client can
   never find the daemon it just spawned.
2. **Setting it too late is just as broken as not setting it at all** -
   confirmed directly via a real, 100%-reproducible frozen-build failure
   ("Jarvis background service didn't start in time", even though the
   daemon's own log showed it starting successfully every time): the GUI
   used to set this default *inside* `jarvis.modules.desktop_app.app.
   main()`'s function body, but `jarvis.modules.daemon.client`'s own
   `_DAEMON_HOME`/`DISCOVERY_FILE`/`LOG_FILE` are plain module-level
   statements, computed once and cached at *import* time - and
   `main_window.py` (imported transitively the moment `app.py` itself is
   imported, via `from jarvis.modules.desktop_app.main_window import
   MainWindow` at `app.py`'s own module level) imports `client.py`
   *before* `app.main()`'s body ever runs. So the client permanently
   cached the wrong fallback path for the GUI's entire process lifetime,
   while the daemon child process (a fresh interpreter, started later,
   correctly inheriting the by-then-set env var) wrote its discovery file
   to the *right* path - two processes, two different, unreconcilable
   ideas of where to look, guaranteed never to agree. Calling
   `_ensure_daemon_home_default()` here, before either branch's first
   import, is the only place early enough to beat every transitive import
   in both directions.

PYTHONHASHSEED pinning is reused directly from `jarvis.cmd.daemon`
(`_ensure_fixed_hash_seed`), not duplicated - that function is already
frozen-aware for exactly this dispatch case (see its own docstring).
Only the daemon branch needs it; the MCP server is a stdio proxy with no
hash-sensitive state of its own.

Every branch is a deferred import, same reasoning as everywhere else in
this codebase that defers a heavy/optional import: none of `--daemon`,
`--mcp`, or a bare GUI invocation should need the other two branches'
dependency stacks (PySide6, the daemon's, mcp's) to even be importable -
loading them when we're about to hand off elsewhere would be pure waste,
and in a frozen build, needlessly increases what has to be bundled to
still function.
"""

import multiprocessing
import os
import sys


def _ensure_daemon_home_default() -> None:
    from jarvis_server.internal.env_config import default_daemon_home

    daemon_home = default_daemon_home()
    if daemon_home:
        os.environ.setdefault("JARVIS_DAEMON_HOME", daemon_home)


def main() -> None:
    if "--daemon" in sys.argv:
        from jarvis.cmd.daemon import _ensure_fixed_hash_seed

        _ensure_fixed_hash_seed()

        from jarvis_server.daemon.server import main as daemon_main

        daemon_main()
    elif "--mcp" in sys.argv:
        _ensure_daemon_home_default()

        from jarvis.cmd.mcp_server import main as mcp_main

        mcp_main()
    else:
        _ensure_daemon_home_default()

        from jarvis.modules.desktop_app.app import main as gui_main

        gui_main()


if __name__ == "__main__":
    # Required first call for any frozen (PyInstaller) app that could ever
    # spawn a real multiprocessing/joblib worker (organize's HDBSCAN step
    # is the current one - see clustering.py's core_dist_n_jobs comment
    # for the real bug this closes: without this guard, a spawned worker
    # re-executes this exact .exe from scratch with no --daemon/--mcp flag
    # in its argv, which falls through to relaunching the full GUI per
    # worker instead of running as a worker. A no-op when not frozen.
    multiprocessing.freeze_support()
    main()
