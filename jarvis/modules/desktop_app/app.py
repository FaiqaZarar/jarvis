"""GUI entrypoint. Only ever called from `jarvis/cmd/desktop_app.py`'s
non-`--daemon`/non-`--mcp` dispatch branch - nothing else should import
this module, since importing it pulls in PySide6.

**`JARVIS_DAEMON_HOME`'s default is set by `desktop_app.py`'s dispatch,
*before* this module is even imported - not here.** It used to be set
here, inside `main()`, and that was a real, confirmed bug: this module's
own top-level `from jarvis.modules.desktop_app.main_window import
MainWindow` transitively imports `jarvis.modules.daemon.client`, whose
`_DAEMON_HOME`/`DISCOVERY_FILE`/`LOG_FILE` are plain module-level
statements computed once, at import time. That import happens the
moment `app.py` itself is imported - which is before `main()`'s body (and
whatever it sets) ever runs. Setting the default inside `main()` was
therefore always too late: the client permanently cached the wrong
fallback path for the process's entire lifetime, while the daemon child
process (started later, correctly inheriting the env var by then)
computed its own, different, correct path - two processes that could
never agree on where to look. See `jarvis.cmd.desktop_app`'s module
docstring for the full story and the real frozen-build failure this
caused.

Also loads `.env` (dev-mode repo-relative first, then the packaged app's
own saved settings at `<JARVIS_DAEMON_HOME>/.env` - see
`jarvis_server.internal.env_config`) into this process's own environment,
before `MainWindow` is constructed - both so `MainWindow` can decide
whether the first-run settings screen is needed at all
(`env_config.has_embedding_api_key()`), and so `_spawn_daemon()`'s
`env=dict(os.environ)` hands the daemon subprocess these values
directly, reinforcing (not replacing) that subprocess's own `.env`
loading (`jarvis_server.daemon.server`).
"""

import sys

from PySide6.QtCore import QThreadPool
from PySide6.QtWidgets import QApplication

from jarvis_server.internal import telemetry
from jarvis_server.internal.env_config import load_env
from jarvis.modules.daemon import client as daemon_client
from jarvis.modules.desktop_app import theme
from jarvis.modules.desktop_app.app_icon import build_app_icon
from jarvis.modules.desktop_app.main_window import MainWindow


def main() -> None:
    app = QApplication(sys.argv)
    app.setApplicationName("Jarvis")
    app.setOrganizationName("Jarvis")
    app.setStyleSheet(theme.build_stylesheet())
    app.setWindowIcon(build_app_icon())

    load_env()
    # The daemon's home, so both processes report under one install ID.
    telemetry.init("jarvis-desktop", daemon_client.DISCOVERY_FILE.parent)
    app.aboutToQuit.connect(telemetry.shutdown)
    telemetry.capture("app_opened")

    pool = QThreadPool.globalInstance()
    window = MainWindow(pool)
    window.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
