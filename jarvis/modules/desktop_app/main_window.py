"""Main window (design doc §2, §4).

Five pages behind a `NavRail` (`views/nav_rail.py`) + `QStackedWidget`,
not a `QTabWidget` - Settings and Stats & Activity need to stay reachable
before the daemon is ready (see below), which a single `setEnabled()`
over one container can no longer express once some pages are
daemon-dependent and others aren't.

Auto-start on launch (§4): `ensure_daemon_running()` runs once, off the
GUI thread, as soon as the window is constructed - `search_view`,
`hierarchy_view` and `connectors_view` each start disabled and are only
enabled once that check succeeds, rather than letting a user click into
Search before there's any daemon to search against. Settings and Stats &
Activity are never gated this way - both need to work even when the
daemon can't start (e.g. fixing a bad API key). A daemon-check failure
shows the same three-tier message (§5) on the shared banner, exactly
like any other read-action failure - the three daemon-dependent pages
stay disabled since nothing under them can do anything useful yet.

Hierarchy reload wiring (confirmed real gap, now fixed): `hierarchy_view.
load_top_level()` used to be called exactly once, right here, at daemon-
ready time - nothing ever called it again, so a tab left open through a
later ingest kept showing stale, pre-ingest data forever. Connecting
`connectors_view.ingest_succeeded` to `hierarchy_view.load_top_level`
here means a successful ingest reloads the hierarchy automatically,
with neither view needing a reference to the other - this window is the
only thing that knows about both.

First-run settings screen (docs/desktop-app-DESIGN.md §8.8): before the
daemon check even runs, `env_config.has_embedding_api_key()` decides
whether an `EMBEDDING_API_KEY` is already configured (dev-mode `.env` or
the packaged app's own saved settings - see that module). If not,
`SettingsDialog` is shown first-run-style; either way (saved or
cancelled), the daemon check proceeds exactly as before - the daemon
itself doesn't need a key to start, only real embed/search calls do,
and those already fail with a clear, existing message if it's missing.
The old `&Settings` menu bar that used to reopen this dialog is gone -
that content now lives on the Settings nav page (`views/settings_view.py`)
instead, sharing its API-field form with this dialog rather than
duplicating it. `_show_first_run_settings` (below) is kept regardless,
since first-run still needs a modal that gates startup, which a page in
the stacked widget can't do.

`_show_claude_desktop_dialog` opens `ConnectClaudeDesktopDialog` - unlike
the daemon-dependent pages and `SettingsDialog`, this doesn't touch the
daemon or `EMBEDDING_API_KEY` at all, it's read-only guidance for
pointing an external MCP client at this app's own `--mcp` entrypoint
(see that dialog's module docstring for the generate-and-copy,
never-auto-write design).

Daemon shutdown on close (real, confirmed bug fix - see jarvis_server.
daemon.server's module docstring for the full mechanism): once the
daemon is confirmed reachable, this window registers itself as a client
(`register_client()`) and remembers the id; `closeEvent()` asks the
daemon to retire that id (`request_shutdown()`), which only actually
stops the daemon if no other registered client (e.g. a `--mcp` session
Claude Desktop still has open) remains. Both calls are best-effort: a
registration failure just means this window won't be able to ask for a
clean shutdown later (nothing else about the window degrades), and
`closeEvent()` swallows any failure from the shutdown request itself -
closing the window must never hang or crash because the daemon happened
to be slow or already gone.

Real, confirmed race this also has to handle: `ensure_daemon_running()`
can take up several seconds on a cold start (design doc §6.1's startup
timeout), and `register_client()` only fires *after* that succeeds - so
there's a real window where a user closes the app before
`_daemon_client_id` is ever set. `closeEvent()` finding `None` there
used to mean nothing retired that registration, ever - the exact orphan
this whole mechanism exists to prevent, just via a narrower door.
`_closing` catches this: if registration finishes after the window is
already gone, `_on_client_registered` retires it itself immediately
instead of leaving it to a `closeEvent()` that already ran.

Window geometry persistence (docs/desktop-app-ux-redesign-DESIGN.md §7,
open question #2): size/position/maximized-state are saved to `QSettings`
on close and restored on the next launch, keyed under `MainWindow/geometry`.
Relies on `app.py`'s `QApplication.setOrganizationName`/`setApplicationName`
calls for where `QSettings()` actually persists to - this class never
names a file/registry path itself.

In-app updater wiring: an automatic, silent check against the public
releases repo (`update_checker.py`) fires once in `__init__`, independent
of the daemon check above - it's a plain GitHub API call, nothing to do
with the daemon. A found update shows `update_banner` (not the daemon's
own `banner` - two distinct concerns); confirming it downloads
(`update_downloader.py`, progress shown in the same banner), then asks
one explicit modal confirmation before handing off to
`update_installer.py` and quitting - see that module's own docstring for
why the actual install/relaunch can't happen from inside this still-running
process. `QApplication.quit` is called through `self._quit_app` rather
than directly, purely so tests can monkeypatch it without affecting every
other test sharing pytest-qt's one process-wide `QApplication`.
"""

from pathlib import Path
from typing import Optional

from PySide6.QtCore import QSettings, QThreadPool, QTimer, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QMainWindow,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from jarvis_server.internal import env_config
from jarvis.modules.cloud import client as cloud_client
from jarvis.modules.cloud import session_store
from jarvis.modules.daemon import client as daemon_client
from jarvis.modules.daemon import process_stats
from jarvis.modules.daemon.client import (
    DaemonError,
    ensure_daemon_running,
    is_daemon_reachable,
    register_client,
    request_shutdown,
)
from jarvis.modules.desktop_app import theme, update_checker, update_downloader, update_installer, version
from jarvis.modules.desktop_app.claude_desktop_dialog import ConnectClaudeDesktopDialog
from jarvis.modules.desktop_app.daemon_worker import run_daemon_call
from jarvis.modules.desktop_app.error_presentation import DaemonStatusBanner, confirm_action
from jarvis.modules.desktop_app.settings_dialog import SettingsDialog
from jarvis.modules.desktop_app.sign_in_dialog import SignInDialog
from jarvis.modules.desktop_app.update_banner import UpdateBanner
from jarvis.modules.desktop_app.views.connectors_view import ConnectorsView
from jarvis.modules.desktop_app.views.hierarchy_view import HierarchyView
from jarvis.modules.desktop_app.views.nav_rail import NavRail
from jarvis.modules.desktop_app.views.search_view import SearchView
from jarvis.modules.desktop_app.views.settings_view import SettingsView
from jarvis.modules.desktop_app.views.stats_activity_view import StatsActivityView


def _settings() -> QSettings:
    # A seam, not an inline QSettings() call: tests monkeypatch this to
    # point at an isolated .ini file instead of the real Windows registry
    # (or the real user's config file on Mac/Linux).
    return QSettings()


class MainWindow(QMainWindow):
    def __init__(self, pool: QThreadPool):
        super().__init__()
        self._pool = pool
        self._daemon_client_id: Optional[str] = None
        self._closing = False
        self._pending_update: Optional[update_checker.UpdateInfo] = None
        self._downloaded_update_path: Optional[Path] = None
        self._update_banner_mode: Optional[str] = None  # None | "download_failed" | "install_failed" | "blocked"
        self._syncing = False
        self._quit_app = QApplication.quit
        # Passive recovery watch (confirmed real gap: nothing previously
        # noticed a daemon that came up on its own after the initial
        # check gave up - the error banner just stayed stale forever).
        # Only ever started from _on_daemon_unreachable, stopped the
        # moment a recheck succeeds - never spawns itself, just watches.
        self._daemon_recheck_timer = QTimer(self)
        self._daemon_recheck_timer.setInterval(3000)
        self._daemon_recheck_timer.timeout.connect(self._recheck_daemon)
        self.setWindowTitle("Jarvis")
        self._restore_geometry()

        self.banner = DaemonStatusBanner()
        self.update_banner = UpdateBanner()
        self.update_banner.update_clicked.connect(self._on_update_confirmed)
        self.update_banner.later_clicked.connect(self._on_update_later)

        self.search_view = SearchView(pool, self.banner)
        self.hierarchy_view = HierarchyView(pool, self.banner)
        self.connectors_view = ConnectorsView(pool)
        self.settings_view = SettingsView(pool, self.banner, self._get_daemon_client_id)
        self.stats_view = StatsActivityView(pool, self.banner)

        self.connectors_view.ingest_succeeded.connect(self.hierarchy_view.load_top_level)
        self.settings_view.check_for_updates_clicked.connect(lambda: self._check_for_updates(manual=True))
        self.settings_view.claude_desktop_dialog_closed.connect(self.connectors_view.refresh_setup_checklist)
        self.search_view.go_to_sources_clicked.connect(self._go_to_sources)
        self.hierarchy_view.go_to_sources_clicked.connect(self._go_to_sources)
        self.connectors_view.go_to_search_clicked.connect(self._go_to_search)
        self.connectors_view.sync_state_changed.connect(self._on_sync_state_changed)

        for view in (self.search_view, self.hierarchy_view, self.connectors_view):
            view.setEnabled(False)

        self.nav_rail = NavRail()
        self.pages = QStackedWidget()
        # Order must match NavRail.NAV_ITEMS exactly - page selection is
        # positional (nav_rail.py's page_selected emits an index), not
        # name-based.
        for page in (
            self.connectors_view, self.search_view, self.hierarchy_view,
            self.stats_view, self.settings_view,
        ):
            self.pages.addWidget(page)
        self.nav_rail.page_selected.connect(self.pages.setCurrentIndex)
        self.connectors_view.sync_state_changed.connect(self.nav_rail.set_syncing)
        self.connectors_view.setup_attention_changed.connect(self.nav_rail.set_needs_attention)

        central = QWidget()
        outer_layout = QHBoxLayout(central)
        outer_layout.addWidget(self.nav_rail)

        # The app-wide banners get the same inset as each page's own body,
        # so they line up with the page banners instead of running into
        # the window edge. While both are hidden the layout is empty, and
        # an empty layout takes no space, margins included.
        banner_layout = QVBoxLayout()
        banner_layout.setContentsMargins(theme.SPACING_XL, theme.SPACING_LG, theme.SPACING_XL, theme.SPACING_LG)
        banner_layout.setSpacing(theme.SPACING_SM)
        banner_layout.addWidget(self.banner)
        banner_layout.addWidget(self.update_banner)

        content_layout = QVBoxLayout()
        content_layout.addLayout(banner_layout)
        content_layout.addWidget(self.pages)
        outer_layout.addLayout(content_layout, 1)

        self.setCentralWidget(central)

        if cloud_client.is_configured() and session_store.load_session() is None:
            if self._show_first_run_sign_in():
                return  # user asked to quit - don't show the settings dialog too

        if env_config.has_embedding_api_key():
            self._check_daemon()
        else:
            self._show_first_run_settings()

        self._check_for_updates(manual=False)

    def _restore_geometry(self) -> None:
        # saveGeometry()/restoreGeometry() round-trip an opaque QByteArray
        # that also captures maximized/full-screen state and which monitor
        # the window was on - plain x/y/width/height wouldn't.
        geometry = _settings().value("MainWindow/geometry")
        if geometry is not None:
            self.restoreGeometry(geometry)

    def _show_first_run_sign_in(self) -> bool:
        """Returns True if the user clicked "Quit Jarvis" - the caller
        stops right there instead of continuing on to the next
        first-run dialog, which would otherwise make Quit look like it
        did nothing (confirmed via a real repro)."""
        # settings_view's Account section was already built (and its label
        # computed) before this runs - refresh it after the dialog closes
        # so a successful sign-in here is actually reflected there, instead
        # of the page silently showing stale "Not signed in" state forever.
        dialog = SignInDialog(self, self._pool, first_run=True)
        dialog.exec()
        self.settings_view.refresh_account_label()
        return dialog.quit_requested

    def _show_first_run_settings(self) -> None:
        SettingsDialog(self, first_run=True).exec()
        self._check_daemon()

    def _show_claude_desktop_dialog(self) -> None:
        ConnectClaudeDesktopDialog(self).exec()

    def _check_daemon(self) -> None:
        run_daemon_call(
            self._pool, ensure_daemon_running,
            on_success=self._on_daemon_ready,
            on_error=self._on_daemon_unreachable,
        )

    def _recheck_daemon(self) -> None:
        run_daemon_call(self._pool, is_daemon_reachable, on_success=self._on_daemon_recheck_result)

    def _on_daemon_recheck_result(self, reachable: bool) -> None:
        if not reachable:
            return  # keep waiting silently - the timer ticks again on its own
        self._on_daemon_ready(0)

    def _on_daemon_ready(self, _port: int) -> None:
        self._daemon_recheck_timer.stop()
        for view in (self.search_view, self.hierarchy_view, self.connectors_view):
            view.setEnabled(True)
        self.banner.dismiss()
        self.hierarchy_view.load_top_level()
        # Signed-in/Claude-Desktop are both synchronous local reads, so
        # this resolves them immediately; has_source stays "Checking…"
        # until connectors_view's own _load_synced_roots (triggered by
        # its showEvent) lands moments later and calls this again.
        self.connectors_view.refresh_setup_checklist()
        run_daemon_call(self._pool, register_client, on_success=self._on_client_registered)

    def _on_daemon_unreachable(self, _exc: Exception) -> None:
        # DESIGN.md §6 gives every tier the same calm GUI message,
        # deliberately not daemon_client's own _USER_MESSAGES text - that
        # dict is shared with the CLI tools (filesystem_ingest.py,
        # drive_ingest.py print it directly), so changing it to this
        # wording would leak GUI-specific copy into terminal output.
        self.banner.show_error(
            "The background service isn't running. Everything already saved is safe.",
            action=("Restart service", self._restart_daemon),
            dismissible=False,
        )
        # Confirmed real gap: nothing previously re-checked after this -
        # a daemon that came up on its own moments later (a slow cold
        # start, not an actual failure) left the banner stuck regardless.
        # Passive only - never spawns, just watches for a recovery that
        # already happened some other way (its own slow startup, or the
        # banner's Restart service, which calls _on_daemon_ready and stops
        # this via the line there).
        self._daemon_recheck_timer.start()

    def _restart_daemon(self) -> None:
        self.banner.show_info("Restarting…")
        run_daemon_call(
            self._pool, process_stats.force_restart_daemon,
            on_success=lambda _result: self._on_daemon_ready(0),
            on_error=self._on_daemon_unreachable,
        )

    def _on_sync_state_changed(self, syncing: bool) -> None:
        self._syncing = syncing

    def _go_to_sources(self) -> None:
        # Sources is index 0 - confirmed by this same __init__'s own
        # page-construction order above.
        self.pages.setCurrentIndex(0)
        self.nav_rail.select_page(0)

    def _go_to_search(self) -> None:
        self.pages.setCurrentIndex(1)
        self.nav_rail.select_page(1)

    def _get_daemon_client_id(self) -> Optional[str]:
        # A callable, not a plain attribute passed by value at
        # construction time: settings_view.py's Restart button needs
        # whatever the current registration is at click time, which is
        # None until _on_client_registered fires and can change again
        # across a later daemon restart.
        return self._daemon_client_id

    def _on_client_registered(self, client_id: str) -> None:
        self._daemon_client_id = client_id
        if self._closing:
            # closeEvent() already ran and found no id to retire -
            # registration only completed after that. Retire it now
            # rather than leaving a phantom registration nothing will
            # ever ask the daemon to release (see module docstring).
            try:
                request_shutdown(client_id)
            except DaemonError:
                pass

    def _check_for_updates(self, *, manual: bool) -> None:
        current = version.get_current_version()
        if current is None:
            if manual:
                self.banner.show_info("Update checking isn't available in a dev build.")
            return
        update_checker.run_update_check(
            self._pool, current,
            on_result=lambda result: self._on_update_check_result(result, manual=manual),
        )

    def _on_update_check_result(self, result: Optional[update_checker.UpdateInfo], *, manual: bool) -> None:
        # Settings' own "checked N ago" label (Figma parity pass, Phase
        # 10) needs every check's outcome, not just manual ones - the
        # automatic startup check is the common case and the only one
        # this page would otherwise never hear about.
        self.settings_view.report_update_check_result(result is not None)
        if result is not None:
            self._pending_update = result
            self._downloaded_update_path = None
            self._update_banner_mode = None
            self.update_banner.show_update_available(result.tag)
        elif manual:
            self.banner.show_success("You're up to date.")

    def _on_update_later(self) -> None:
        # The same later_clicked signal also drives "Download manually"
        # on an install-failed banner and "Dismiss" on a blocked one
        # (UpdateBanner only renders; this is where the two meanings are
        # told apart) - the banner itself always fades out regardless,
        # via UpdateBanner._on_later.
        if self._update_banner_mode == "install_failed":
            QDesktopServices.openUrl(QUrl(update_checker.releases_page_url()))
            return
        if self._update_banner_mode == "blocked":
            return
        self._pending_update = None
        self._downloaded_update_path = None

    def _on_update_confirmed(self) -> None:
        # update_clicked means "start the download" the first time
        # through, but also "restart now" once Ready, and "retry" after
        # either a download or install failure - MainWindow is the only
        # thing that knows which, from what's still pending.
        if self._downloaded_update_path is not None:
            self._install_update(self._downloaded_update_path)
            return
        if self._pending_update is None:
            return
        self._update_banner_mode = None
        self.update_banner.show_downloading()
        daemon_home = daemon_client.DISCOVERY_FILE.parent
        update_downloader.run_update_download(
            self._pool, self._pending_update, daemon_home,
            on_progress=self.update_banner.set_progress,
            on_success=self._on_update_downloaded,
            on_error=self._on_update_download_failed,
        )

    def _on_update_downloaded(self, update_path: Path) -> None:
        self._downloaded_update_path = update_path
        self._update_banner_mode = None
        self.update_banner.show_ready()

    def _install_update(self, update_path: Path) -> None:
        if self._syncing and not confirm_action(
            self, "Restart now?",
            "A sync is running and will stop. You can retry it after the update.",
            "Restart now",
        ):
            return
        self.update_banner.show_installing()
        QApplication.processEvents()  # the process exits moments after trigger_install_and_relaunch returns
        try:
            update_installer.trigger_install_and_relaunch(update_path, self._daemon_client_id)
        except update_installer.UpdateBlockedError as exc:
            self._update_banner_mode = "blocked"
            self.update_banner.show_blocked(str(exc))
            return
        except RuntimeError:
            self._update_banner_mode = "install_failed"
            self.update_banner.show_install_failed()
            return
        self._quit_app()

    def _on_update_download_failed(self, _exc: Exception) -> None:
        self._update_banner_mode = "download_failed"
        self.update_banner.show_download_failed()

    def closeEvent(self, event) -> None:
        _settings().setValue("MainWindow/geometry", self.saveGeometry())
        self._closing = True
        self._daemon_recheck_timer.stop()
        if self._daemon_client_id is not None:
            try:
                request_shutdown(self._daemon_client_id)
            except DaemonError:
                # Best-effort cleanup - closing the window must never hang
                # or fail because the daemon happened to be slow, or is
                # already gone. Either way, this window is closing regardless.
                pass
        super().closeEvent(event)
