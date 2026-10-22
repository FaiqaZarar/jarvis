"""Minimal manual-verification entry point for the desktop app skeleton
(docs/desktop-app-DESIGN.md) - **not** a packaging entry point.

`jarvis/cmd/desktop_app.py` is the real one (argv-dispatched: a
`--daemon` flag routes to the daemon instead of the GUI - the thing
PyInstaller will eventually freeze). This script exists purely so
`python run_desktop_app.py`, from an activated venv, opens the actual
window for real manual testing right now, with no argv-dispatch
indirection to reason about and no packaging step involved.

Nothing is reimplemented here: `jarvis.modules.desktop_app.app.main()`
already does everything needed - defaults `JARVIS_DAEMON_HOME`,
constructs the `QApplication`, builds `MainWindow` (which calls
`ensure_daemon_running()` itself, automatically, as soon as it's
constructed - see that module for the actual auto-start wiring).
"""

from jarvis.modules.desktop_app.app import main

if __name__ == "__main__":
    main()
