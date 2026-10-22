"""Settings page (plan doc §8) - the persistent home for everything that
used to live in the menu-bar `SettingsDialog` (API/embedding config,
"Connect Claude Desktop…"), plus new daemon health/control, RAM/storage,
and update-check surfaces. `SettingsDialog` itself is unchanged and still
used for first-run (see `main_window.py`'s docstring for why a modal has
to gate startup, when a persistent page can't) - the two share the same
API-field `QFormLayout` construction via `settings_dialog.
build_embedding_fields()` rather than duplicating it.

This view does not own the update-check state machine: `MainWindow`
already has `_check_for_updates`/`_on_update_check_result`/etc., and the
banner/installer/quit calls those drive are naturally `MainWindow`-level
(this page isn't the only thing on screen when an update finishes
downloading). `check_for_updates_clicked` is the seam - `MainWindow`
connects it to `self._check_for_updates(manual=True)`, the same call the
old menu action used to make.

Every daemon-touching call (status refresh, Restart, Force Kill) goes
through `run_daemon_call`, never inline on the GUI thread - `process_stats.
get_daemon_pid()` alone is a real HTTP health check (see that module and
`daemon_client._live_port`), not a free local read.

RAM/storage refresh on a `QTimer`, started/stopped from `showEvent`/
`hideEvent` (this page's own visibility, not `MainWindow`'s stacked-widget
index) - the same busy-while-visible shape as `ConnectorsView`'s ingest-
status poller, just gated on page visibility instead of an in-flight
operation.
"""

import os
from datetime import datetime
from typing import Callable, Optional

from PySide6.QtCore import QThreadPool, QTimer, Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QCheckBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from jarvis.modules.cloud import client as cloud_client
from jarvis.modules.cloud import session_store
from jarvis.modules.daemon import client as daemon_client
from jarvis.modules.daemon import process_stats
from jarvis.modules.daemon.client import DaemonError, ensure_daemon_running, request_shutdown
from jarvis.modules.desktop_app import claude_desktop_setup as cds
from jarvis.modules.desktop_app import format_utils, setup_health, theme, version
from jarvis.modules.desktop_app.claude_desktop_dialog import ConnectClaudeDesktopDialog
from jarvis.modules.desktop_app.collapsible_section import CollapsibleSection
from jarvis.modules.desktop_app.daemon_worker import run_daemon_call
from jarvis.modules.desktop_app.error_presentation import DaemonStatusBanner, confirm_action
from jarvis.modules.desktop_app.feedback_dialog import FeedbackDialog
from jarvis.modules.desktop_app.page_header import PageHeader
from jarvis.modules.desktop_app.settings_dialog import build_embedding_fields
from jarvis.modules.desktop_app.sign_in_dialog import SignInDialog
from jarvis_server.internal import env_config

_POLL_INTERVAL_MS = 2500

# DESIGN.md §6 (Failures) - never the raw exception text (§5.4).
_SERVICE_UNREACHABLE_MESSAGE = "The background service isn't running. Everything already saved is safe."
_UNKNOWN_MESSAGE = "Something went wrong. Everything already saved is safe."

# Claude connection section (Figma parity pass, Phase 10) - Settings'
# own wording, distinct from the Sources checklist row's (Phase 8) -
# this is the persistent "what's the real current state" surface, not
# an onboarding step. "not_connected" also covers "isn't installed" and
# "cannot connect here" (translocated) - same fold-in `setup_health.py`
# already documents, for the same reason (no reliable signal to tell
# "never configured" apart from "not installed").
_CLAUDE_STATUS_MESSAGES = {
    "connected": "Claude Desktop can use your files.",
    "needs_repair": "The Claude connection needs repair.",
    "translocated": "Move Jarvis to your Applications folder and reopen it.",
    "not_connected": "Claude Desktop isn't connected yet.",
}
# The one action for each state (DESIGN.md §6): Connect when it isn't set
# up, Repair when it's broken, nothing once it works.
_CLAUDE_ACTION_LABELS = {
    "not_connected": "Connect",
    "needs_repair": "Repair",
    "translocated": "Repair",
}
_CLAUDE_STATUS_PILL_TEXT = {
    "connected": "Connected",
    "needs_repair": "Needs repair",
    "translocated": "Needs repair",
    "not_connected": "Not connected",
}


def _restart_and_respawn(client_id: Optional[str]) -> None:
    """Graceful restart: retire the current registration (best-effort,
    same as `MainWindow.closeEvent` - a dead/unreachable daemon has
    nothing to retire anyway), then reuse the ordinary auto-start flow to
    bring a fresh one up. Run entirely on a worker thread via
    `run_daemon_call` - both halves are real HTTP round-trips."""
    if client_id is not None:
        try:
            request_shutdown(client_id)
        except DaemonError:
            pass
    ensure_daemon_running()


def _gather_daemon_status() -> dict:
    pid = process_stats.get_daemon_pid()
    ram = process_stats.get_process_ram(pid) if pid is not None else None
    uptime = process_stats.get_daemon_uptime_seconds(pid) if pid is not None else None
    return {
        "pid": pid,
        "ram": ram,
        "uptime": uptime,
        "storage": process_stats.get_storage_usage(),
    }


class SettingsView(QWidget):
    check_for_updates_clicked = Signal()
    claude_desktop_dialog_closed = Signal()

    def __init__(
        self,
        pool: QThreadPool,
        banner: DaemonStatusBanner,
        get_daemon_client_id: Callable[[], Optional[str]],
        parent: QWidget = None,
    ):
        super().__init__(parent)
        self._pool = pool
        self._banner = banner
        self._get_daemon_client_id = get_daemon_client_id
        # Read in showEvent/_refresh_updates_label before the first real
        # check ever completes - set before any _build_* call below,
        # since _build_updates_group() renders from these immediately.
        self._last_checked_at: Optional[datetime] = None
        self._last_check_found_update: Optional[bool] = None

        # Figma parity pass (Phase 10): the real reference shows four
        # sections (Account, API key, Claude connection, Updates) - the
        # daemon/RAM/storage/log diagnostics this page already had don't
        # belong beneath them, loose (DESIGN.md principle 7, "complexity
        # stays beneath the surface") - they move into one collapsed-by-
        # default Advanced section instead of disappearing outright.
        api_group, advanced_fields = self._build_api_group()
        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(
            theme.SPACING_XL, theme.SPACING_LG, theme.SPACING_XL, theme.SPACING_LG,
        )
        # Account and feedback both go through the cloud server, which a
        # build from source may not have (cloud_client.is_configured()).
        if cloud_client.is_configured():
            content_layout.addWidget(self._build_account_group())
            content_layout.addWidget(self._build_feedback_group())
        content_layout.addWidget(self._build_privacy_group())
        content_layout.addWidget(api_group)
        content_layout.addWidget(self._build_claude_connection_group())
        content_layout.addWidget(self._build_updates_group())
        content_layout.addWidget(self._build_advanced_section(advanced_fields))
        content_layout.addStretch(1)

        scroll_area = QScrollArea()
        scroll_area.setWidgetResizable(True)
        scroll_area.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll_area.setWidget(content)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(PageHeader("Settings"))
        layout.addWidget(scroll_area, 1)

        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(_POLL_INTERVAL_MS)
        self._poll_timer.timeout.connect(self._refresh_status)
        # Keeps the Updates label's relative-time portion ("checked 2
        # minutes ago") current while the page stays open - piggybacks
        # on the existing poll timer rather than a second one.
        self._poll_timer.timeout.connect(self._refresh_updates_label)

    # -- section builders --------------------------------------------

    def _build_account_group(self) -> QGroupBox:
        group = QGroupBox("Account")
        self._account_label = QLabel("")
        self._account_button = QPushButton("")
        self._account_button.clicked.connect(self._on_account_button_clicked)
        self.refresh_account_label()

        group_layout = QVBoxLayout(group)
        group_layout.addWidget(self._account_label)
        group_layout.addWidget(self._account_button, 0, Qt.AlignmentFlag.AlignLeft)
        return group

    def _build_api_group(self) -> tuple[QGroupBox, QWidget]:
        """`(main section, advanced fields widget)` - the API key field
        stays in the primary view (Figma parity pass, Phase 10); Model/
        API base move into the caller's Advanced section. All three
        still come from the one shared `build_embedding_fields()`
        construction `SettingsDialog` also uses - only its combined
        `QFormLayout` return value is discarded here (never attached to
        anything, so nothing is lost by not using it); the field
        widgets themselves are reparented into two fresh layouts
        instead, one per section."""
        current = env_config.read_saved_settings()
        _unused_form, self._api_key, self._model, self._api_base = build_embedding_fields(current)

        group = QGroupBox("API key")
        helper_label = QLabel("Jarvis uses a shared key during testing. You can add your own.")
        helper_label.setWordWrap(True)
        save_button = QPushButton("Save")
        save_button.setProperty("buttonKind", "primary")
        save_button.clicked.connect(self._on_save_clicked)
        key_row = QHBoxLayout()
        key_row.addWidget(self._api_key, 1)
        key_row.addWidget(save_button)

        group_layout = QVBoxLayout(group)
        group_layout.addWidget(helper_label)
        group_layout.addLayout(key_row)

        advanced_fields = QWidget()
        advanced_form = QFormLayout(advanced_fields)
        advanced_form.addRow("Embedding model:", self._model)
        advanced_form.addRow("API base URL:", self._api_base)
        return group, advanced_fields

    def _build_claude_connection_group(self) -> QGroupBox:
        group = QGroupBox("Claude connection")
        self._claude_status_label = QLabel("")
        self._claude_status_label.setWordWrap(True)
        self._claude_status_pill = QLabel("")
        self._claude_status_pill.setObjectName("settingsStatusPill")

        status_row = QHBoxLayout()
        status_row.addWidget(self._claude_status_label, 1)
        status_row.addWidget(self._claude_status_pill)

        self._claude_action_button = QPushButton()
        self._claude_action_button.clicked.connect(self._on_connect_claude_desktop_clicked)

        buttons_row = QHBoxLayout()
        buttons_row.addWidget(self._claude_action_button)
        buttons_row.addStretch(1)

        group_layout = QVBoxLayout(group)
        group_layout.addLayout(status_row)
        group_layout.addLayout(buttons_row)
        self.refresh_claude_status()
        return group

    def _build_advanced_section(self, advanced_fields: QWidget) -> CollapsibleSection:
        section = CollapsibleSection("Advanced")
        section.add_widget(advanced_fields)
        section.add_widget(self._build_daemon_group())
        section.add_widget(self._build_ram_group())
        section.add_widget(self._build_storage_group())
        section.add_widget(self._build_log_group())
        return section

    def _build_feedback_group(self) -> QGroupBox:
        group = QGroupBox("Feedback")
        feedback_button = QPushButton("Send feedback")
        feedback_button.clicked.connect(self._on_send_feedback_clicked)

        group_layout = QVBoxLayout(group)
        group_layout.addWidget(feedback_button, 0, Qt.AlignmentFlag.AlignLeft)
        return group

    def _build_privacy_group(self) -> QGroupBox:
        group = QGroupBox("Privacy")
        self._telemetry_checkbox = QCheckBox("Share usage and diagnostics")
        self._telemetry_checkbox.setChecked(env_config.telemetry_enabled())
        self._telemetry_checkbox.toggled.connect(self._on_telemetry_toggled)
        helper_label = QLabel(
            "Jarvis shares error reports and usage counts to help fix problems. Never your files or "
            "searches. A change applies the next time Jarvis starts."
        )
        helper_label.setWordWrap(True)

        group_layout = QVBoxLayout(group)
        group_layout.addWidget(self._telemetry_checkbox)
        group_layout.addWidget(helper_label)
        return group

    def _build_daemon_group(self) -> QGroupBox:
        group = QGroupBox("Background service")
        self._daemon_status_label = QLabel("Checking…")
        self._daemon_pid_label = QLabel("")
        self._daemon_uptime_label = QLabel("")

        self._restart_button = QPushButton("Restart")
        self._restart_button.clicked.connect(self._on_restart_clicked)
        self._force_kill_button = QPushButton("Force kill")
        self._force_kill_button.setProperty("buttonKind", "destructive")
        self._force_kill_button.clicked.connect(self._on_force_kill_clicked)

        buttons_row = QHBoxLayout()
        buttons_row.addWidget(self._restart_button)
        buttons_row.addWidget(self._force_kill_button)
        buttons_row.addStretch(1)

        group_layout = QVBoxLayout(group)
        group_layout.addWidget(self._daemon_status_label)
        group_layout.addWidget(self._daemon_pid_label)
        group_layout.addWidget(self._daemon_uptime_label)
        group_layout.addLayout(buttons_row)
        return group

    def _build_ram_group(self) -> QGroupBox:
        group = QGroupBox("RAM usage")
        self._app_ram_label = QLabel("")
        self._daemon_ram_label = QLabel("")

        group_layout = QVBoxLayout(group)
        group_layout.addWidget(self._app_ram_label)
        group_layout.addWidget(self._daemon_ram_label)
        return group

    def _build_storage_group(self) -> QGroupBox:
        group = QGroupBox("Storage used")
        self._storage_label = QLabel("")

        group_layout = QVBoxLayout(group)
        group_layout.addWidget(self._storage_label)
        return group

    def _build_updates_group(self) -> QGroupBox:
        group = QGroupBox("Updates")
        self._version_text = version.get_current_version() or "Development build"
        self._updates_label = QLabel("")
        self._updates_label.setWordWrap(True)
        self._check_updates_button = QPushButton("Check for updates")
        self._check_updates_button.clicked.connect(self.check_for_updates_clicked)

        group_layout = QVBoxLayout(group)
        group_layout.addWidget(self._updates_label)
        group_layout.addWidget(self._check_updates_button, 0, Qt.AlignmentFlag.AlignLeft)
        self._refresh_updates_label()
        return group

    def _build_log_group(self) -> QGroupBox:
        group = QGroupBox("Logs")
        reveal_button = QPushButton("Reveal log file")
        reveal_button.clicked.connect(self._on_reveal_log_clicked)

        group_layout = QVBoxLayout(group)
        group_layout.addWidget(reveal_button, 0, Qt.AlignmentFlag.AlignLeft)
        return group

    # -- visibility-gated polling --------------------------------------

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._refresh_status()
        self.refresh_claude_status()
        if not self._poll_timer.isActive():
            self._poll_timer.start()

    def hideEvent(self, event) -> None:
        super().hideEvent(event)
        self._poll_timer.stop()

    # -- Account -----------------------------------------------------------

    def refresh_account_label(self) -> None:
        if not cloud_client.is_configured():
            return  # no Account section to refresh
        session = session_store.load_session()
        if session is None:
            self._account_label.setText("Not signed in")
            self._account_button.setText("Sign in")
        else:
            email, _token = session
            self._account_label.setText(f"Signed in as {email}")
            self._account_button.setText("Sign out")

    def _on_account_button_clicked(self) -> None:
        if session_store.load_session() is None:
            SignInDialog(self, self._pool, first_run=False).exec()
        else:
            session_store.clear_session()
        self.refresh_account_label()

    # -- API & Embedding ------------------------------------------------

    def _on_save_clicked(self) -> None:
        api_key = self._api_key.text().strip()
        if not api_key:
            QMessageBox.warning(self, "API key required", "Enter an API key to save.")
            return
        env_config.save_settings(
            api_key,
            self._model.text().strip() or env_config.DEFAULT_MODEL,
            self._api_base.text().strip(),
        )
        self._banner.show_success(
            "Saved. Restart the background service to apply.",
            action=("Restart service", self._on_restart_clicked),
        )

    # -- Claude connection --------------------------------------------------

    def _on_connect_claude_desktop_clicked(self) -> None:
        ConnectClaudeDesktopDialog(self).exec()
        self.refresh_claude_status()
        self.claude_desktop_dialog_closed.emit()

    def refresh_claude_status(self) -> None:
        """Local, synchronous reads only (`claude_desktop_setup.build_plan()`
        touches no daemon) - safe to call from `showEvent` and right
        after the connect/repair dialog closes, same convention as
        `ConnectorsView.refresh_setup_checklist()`. `claude_dismissed`
        is always `False` here, unlike the Sources checklist's own
        version of this state - Settings is the one place that always
        shows the real current connection, regardless of whether the
        onboarding checklist step was dismissed."""
        health = setup_health.compute_setup_health(
            signed_in=True, has_source=True, claude_plan=cds.build_plan(), claude_dismissed=False,
        )
        state = health.claude_desktop
        self._claude_status_label.setText(
            _CLAUDE_STATUS_MESSAGES.get(state, _CLAUDE_STATUS_MESSAGES["not_connected"])
        )
        pill_kind = "connected" if state == "connected" else (
            "not_connected" if state == "not_connected" else "needs_repair"
        )
        self._claude_status_pill.setText(_CLAUDE_STATUS_PILL_TEXT.get(state, "Not connected"))
        self._claude_status_pill.setProperty("tagKind", pill_kind)
        self._claude_status_pill.style().unpolish(self._claude_status_pill)
        self._claude_status_pill.style().polish(self._claude_status_pill)
        action_label = _CLAUDE_ACTION_LABELS.get(state)
        self._claude_action_button.setText(action_label or "")
        self._claude_action_button.setVisible(action_label is not None)

    # -- Feedback -----------------------------------------------------------

    def _on_send_feedback_clicked(self) -> None:
        FeedbackDialog(self, self._pool).exec()
        self.refresh_account_label()  # signing in from inside FeedbackDialog updates this too

    # -- Privacy -----------------------------------------------------------

    def _on_telemetry_toggled(self, enabled: bool) -> None:
        try:
            env_config.save_telemetry_enabled(enabled)
        except (OSError, RuntimeError):
            self._telemetry_checkbox.blockSignals(True)
            self._telemetry_checkbox.setChecked(not enabled)
            self._telemetry_checkbox.blockSignals(False)
            self._banner.show_error("This setting couldn't be saved. Nothing was changed.")
            return
        self._banner.show_success(
            "Saved. Jarvis will share usage and diagnostics from the next start."
            if enabled else "Saved. Jarvis will stop sharing usage and diagnostics from the next start."
        )

    # -- Daemon status card ----------------------------------------------

    def _refresh_status(self) -> None:
        run_daemon_call(self._pool, _gather_daemon_status, on_success=self._on_status_gathered)

    def _on_status_gathered(self, status: dict) -> None:
        pid = status["pid"]
        if pid is None:
            self._daemon_status_label.setText("Stopped")
            self._daemon_pid_label.setText("")
            self._daemon_uptime_label.setText("")
            self._daemon_ram_label.setText("Service RAM: —")
        else:
            self._daemon_status_label.setText("Running")
            self._daemon_pid_label.setText(f"PID: {pid}")
            uptime = status["uptime"]
            self._daemon_uptime_label.setText(
                f"Uptime: {format_utils.format_duration(uptime)}" if uptime is not None else ""
            )
            ram = status["ram"]
            self._daemon_ram_label.setText(
                f"Service RAM: {format_utils.format_bytes(ram)}" if ram is not None else "Service RAM: —"
            )

        app_ram = process_stats.get_process_ram(os.getpid())
        self._app_ram_label.setText(
            f"App RAM: {format_utils.format_bytes(app_ram)}" if app_ram is not None else "App RAM: —"
        )

        storage = status["storage"]
        self._storage_label.setText(
            f"Database: {format_utils.format_bytes(storage['db_bytes'])}   "
            f"Graph: {format_utils.format_bytes(storage['graph_bytes'])}   "
            f"Total: {format_utils.format_bytes(storage['total_bytes'])}"
        )

    def _on_restart_clicked(self) -> None:
        if not confirm_action(
            self, "Restart the background service?",
            "Jarvis is unavailable for a few seconds while it restarts. A sync in progress stops.",
            "Restart",
        ):
            return
        self._restart()

    def _restart(self) -> None:
        client_id = self._get_daemon_client_id()
        self._set_restarting(True)
        run_daemon_call(
            self._pool, _restart_and_respawn, client_id,
            on_success=lambda _r: self._on_daemon_action_done("Background service restarted."),
            on_error=lambda exc: self._on_daemon_action_failed(exc, retry=self._restart),
        )

    def _on_force_kill_clicked(self) -> None:
        if not confirm_action(
            self, "Force kill the background service?",
            "Jarvis stops it immediately, without letting it finish its current step, then starts it "
            "again. A sync in progress stops.",
            "Force kill", destructive=True,
        ):
            return
        self._force_kill()

    def _force_kill(self) -> None:
        # Force Kill auto-respawns (the plan's resolved decision) rather
        # than leaving the service down until something else calls it.
        run_daemon_call(
            self._pool, process_stats.force_restart_daemon,
            on_success=lambda _r: self._on_daemon_action_done("Background service force-killed and restarted."),
            on_error=lambda exc: self._on_daemon_action_failed(exc, retry=self._force_kill),
        )

    def _restart_service(self) -> None:
        """The banner's "Restart service": the service is already down, so
        no confirm and nothing to shut down gracefully - the same call
        MainWindow's own banner makes."""
        self._set_restarting(True)
        self._banner.show_info("Restarting…")
        run_daemon_call(
            self._pool, process_stats.force_restart_daemon,
            on_success=lambda _r: self._on_daemon_action_done("Background service restarted."),
            on_error=lambda exc: self._on_daemon_action_failed(exc, retry=self._restart_service),
        )

    def _set_restarting(self, restarting: bool) -> None:
        self._restart_button.setEnabled(not restarting)
        self._restart_button.setText("Restarting…" if restarting else "Restart")

    def _on_daemon_action_done(self, message: str) -> None:
        self._set_restarting(False)
        self._banner.show_success(message)
        self._refresh_status()

    def _on_daemon_action_failed(self, exc: Exception, retry: Callable[[], None]) -> None:
        """`retry` re-runs whichever action failed."""
        self._set_restarting(False)
        if isinstance(exc, DaemonError):
            # DESIGN.md §4.2: stays until resolved, as MainWindow's does.
            self._banner.show_error(
                _SERVICE_UNREACHABLE_MESSAGE, action=("Restart service", self._restart_service),
                dismissible=False,
            )
        else:
            self._banner.show_error(
                _UNKNOWN_MESSAGE, action=("Retry", retry),
                secondary_action=(
                    ("Send feedback", self._on_send_feedback_clicked) if cloud_client.is_configured() else None
                ),
            )

    # -- Reveal log file --------------------------------------------------

    def _on_reveal_log_clicked(self) -> None:
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(daemon_client.LOG_FILE.parent)))

    # -- Updates --------------------------------------------------------

    def report_update_check_result(self, found_update: bool) -> None:
        """Called by `MainWindow` for *every* update check that
        resolves, automatic or manual - the only way this page can know
        about the common case (the automatic startup check) without
        running a second, duplicate check itself."""
        self._last_checked_at = datetime.now()
        self._last_check_found_update = found_update
        self._refresh_updates_label()

    def _refresh_updates_label(self) -> None:
        if self._last_checked_at is None:
            self._updates_label.setText(self._version_text)
            return
        checked_text = format_utils.format_relative_time(self._last_checked_at.isoformat())
        headline = "An update is available." if self._last_check_found_update else "You're up to date."
        self._updates_label.setText(f"{headline} {self._version_text}, checked {checked_text}.")
