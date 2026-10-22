"""Connectors / ingest view (design doc §2c; live progress feedback per
docs/desktop-app-ux-redesign-DESIGN.md §4; Google Drive per
docs/desktop-app-drive-connector-DESIGN.md).

Filesystem ingest is built out fully this pass: `QFileDialog.
getExistingDirectory()` -> `run_ingest_filesystem(path)` - there's no
picker to build (§1c: the CLI takes a plain path argument too) and
nothing daemon- or connector-specific stands in the way.

**Drive ingest** (docs/desktop-app-drive-connector-DESIGN.md, all open
questions there resolved - OAuth client bundled, per §4): "Connect
Google Drive…" calls `drive.get_drive_service(allow_interactive_auth=True)`
through the same `run_daemon_call` worker-thread seam every other
potentially-slow call in this app already uses (design doc §2 -
`run_daemon_call` has no daemon-specific logic in its own
implementation, nothing about reusing it for a non-daemon callable
needs a rename). OAuth sign-in and folder listing stay entirely
client-side, never daemon-routed - `flow.run_local_server()` has to
open a browser and block in *this* process's foreground, which a
background daemon has no way to do on a caller's behalf (see
`drive_ingest.py`'s own module docstring, and that design doc's §1a,
for the full reasoning). Only the final `run_ingest_drive(folder_id)`
call is daemon-routed, through the *exact* `_start_ingest` path already
built for filesystem ingest below - `_start_ingest` already takes
`(ingest_fn, *args)` generically, so Drive's confirmed folder selection
gets the same busy-button/progress-bar/status-polling behavior for
free, not a second implementation of it.

No early-cancel affordance for the OAuth wait (design doc §2/§7,
accepted default for v1): once "Connect Google Drive…" is clicked, the
worker thread runs until consent completes or `auth.py`'s own 300s
`_OAUTH_LOCAL_SERVER_TIMEOUT_S` elapses - a user who wants to abandon
it waits out the timeout, the same as the CLI already does.

The "list of what's already been ingested" surface from §2c is now the
"Synced Folders" group below - `GET /sync_roots`, most recently synced
first. Resync reuses `_start_ingest` directly, the exact same
stepper/banner/polling/cancel path "Add a folder…"/"Connect Google
Drive…" already drive, not a second implementation. Per-folder file
drill-down (which files a root actually pulled in) stays deferred.

A root that turns out to be an ancestor of an already-tracked one
supersedes it (`Connector.covered_root_ids`, `pipeline.run_ingest`) -
the narrower root is tombstoned out of `sync_roots` once the broader
sync succeeds, so it stops appearing here. The reverse - syncing a
subfolder of an already-synced parent - is allowed and kept as its own
row, rendered nested under its ancestor (`_synced_roots_parent_index`)
purely for readability; only filesystem roots can nest, since root_id
is a plain string for Drive with no derivable containment relationship.

**Live ingest feedback (the redesign's confirmed real gap, now fixed):**
clicking "Add a folder…" used to give zero visible feedback for the
entire, possibly multi-minute duration of a real ingest - the button
stayed clickable, nothing on screen changed, until the result (or a
failure dialog) eventually appeared. `_start_ingest` now disables the
button, shows progress inside the syncing row, and
starts a `QTimer` polling `daemon_client.get_ingest_status()` (the
daemon-side phase tracker) for the duration. Polling goes through the
same `run_daemon_call` seam every other daemon call in this app already
uses, off the GUI thread, not a bespoke mechanism. `_stop_polling()`
(called from both `_on_ingest_result` and `_on_ingest_error`) always
stops the timer before the button re-enables, and `_on_status_polled`
bails out if the timer's already been stopped - a poll tick already in
flight when the main ingest call resolves would otherwise land after
the fact and briefly overwrite the final state with a stale phase.

**Row progress + cancel** (Stitch "Sources - Syncing"): the syncing row
carries its own progress panel - the current phase's text with its live
item counts (`format_phase_progress`, from `progress` in `GET
/ingest/status`) and a bar filled from the same counts
(`_progress_fraction`), indeterminate when a phase reports no total.
The banner names the folder being ingested for the whole run, and flags
files sync couldn't read as soon as the first one fails - otherwise a
one-file folder whose only file failed looks identical to a working
ingest until the summary lands. The row's Cancel calls
`daemon_client.cancel_ingest()` - cooperative, not instant (server.py's
own docstring), so the row reads "Cancelling…" rather than claiming the
ingest has actually stopped yet; the ingest call's own eventual
`{"cancelled": true, ...}` response is what confirms that
(`_on_ingest_result`).

`_poll_ingest_status` passes an explicit `on_error` (`_on_status_poll_failed`,
a no-op) rather than relying on `run_daemon_call`'s implicit "no
on_error means a failed poll is silently dropped" default - investigated
after a real manual-testing report of `GET /ingest/status` occasionally
returning an empty body (client.py's `call()` already converts that into
a retried-then-raised `DaemonConnectionError`, never a raw, unhandled
exception). No reproduction of an actual empty response was found after
extensive testing of the route itself and the daemon's HTTP layer under
real concurrent load - the route has exactly one, unconditional,
always-valid-JSON return path. Hardened anyway: the daemon route now
reads `_ingest_status.get("phase")`, not `["phase"]` (server.py), and
this view now makes "a poll failure is a harmless, silently-skipped
tick" an explicit statement instead of an implicit consequence of
omitting a callback - one poll timer's worth of resilience should not
depend on a reader tracing into daemon_worker.py to confirm it.
"""

import os
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QThreadPool, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QProgressBar,
    QDialog,
    QFileDialog,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from jarvis.modules.cloud import client as cloud_client
from jarvis.modules.cloud import session_store
from jarvis.modules.daemon import client as daemon_client
from jarvis.modules.daemon import process_stats
from jarvis.modules.daemon.client import DaemonError
from jarvis.modules.desktop_app import add_source_dialog
from jarvis.modules.desktop_app import claude_desktop_setup as cds
from jarvis.modules.desktop_app import (
    empty_state, error_reporting, format_utils, page_header, setup_checklist, setup_health, theme,
)
from jarvis.modules.desktop_app.add_source_dialog import AddSourceDialog
from jarvis.modules.desktop_app.claude_desktop_dialog import ConnectClaudeDesktopDialog
from jarvis.modules.desktop_app.daemon_worker import run_daemon_call
from jarvis.modules.desktop_app.drive_folder_dialog import DriveFolderDialog
from jarvis.modules.desktop_app.error_presentation import DaemonStatusBanner, confirm_action
from jarvis.modules.desktop_app.feedback_dialog import FeedbackDialog
from jarvis.modules.desktop_app.settings_dialog import SettingsDialog
from jarvis.modules.desktop_app.sign_in_dialog import SignInDialog
from jarvis_server import pipeline as pipeline_mod
from jarvis_server.connectors import drive
from jarvis_server.mcp_tools.models import ErrorResponse, SyncRootRecord, SyncRootsResult

_POLL_INTERVAL_MS = 750

# DESIGN.md §6 (Failures) - for failures that never reached the background
# service, so it couldn't word them itself.
_SERVICE_UNREACHABLE_MESSAGE = "The background service isn't running. Everything already saved is safe."
_UNKNOWN_CODE = "unknown"
_UNKNOWN_MESSAGE = "Something went wrong. Everything already saved is safe."
# A Drive root synced before folder names were stored, until its next sync.
_DRIVE_FOLDER_FALLBACK_NAME = "Google Drive folder"

# What each sync step is called on screen (DESIGN.md §3, §5.2): the
# pipeline's own phase names ("Generating embeddings") are internal.
_PHASE_LABELS = {
    pipeline_mod.PHASE_READING_FILES: "Reading files",
    pipeline_mod.PHASE_PROCESSING_DOCUMENTS: "Preparing files",
    pipeline_mod.PHASE_GENERATING_EMBEDDINGS: "Making files searchable",
    pipeline_mod.PHASE_ORGANIZING: "Organizing topics",
    pipeline_mod.PHASE_RECONCILING: "Finishing up",
    pipeline_mod.PHASE_DONE: "Finishing up",
}
# Steps whose counts fill the bar. Only Reading files' counts are files,
# so it's the only step that shows them as words; the others count
# passages or topic groups, which mean nothing to the user.
_BAR_PHASES = {
    pipeline_mod.PHASE_READING_FILES,
    pipeline_mod.PHASE_PROCESSING_DOCUMENTS,
    pipeline_mod.PHASE_GENERATING_EMBEDDINGS,
    pipeline_mod.PHASE_ORGANIZING,
}


def _progress_fraction(phase: str, progress: Optional[dict]) -> Optional[float]:
    """0..1 for the row's bar, or None (an indeterminate bar) when this
    phase reports no total. Pure, so it's testable without Qt."""
    if not progress or phase not in _BAR_PHASES or not progress.get("total"):
        return None
    return min(1.0, progress["processed"] / progress["total"])


def _files_phrase(count: int) -> str:
    return "1 file" if count == 1 else f"{count} files"


def format_phase_progress(phase: str, progress: Optional[dict]) -> str:
    """The words beside a syncing row's bar for one `GET /ingest/status`
    poll ("Reading files, 310 of 500"). Pure, so it's testable without Qt."""
    label = _PHASE_LABELS.get(phase, "Syncing")
    if phase == pipeline_mod.PHASE_READING_FILES and progress:
        processed, total = progress["processed"], progress["total"]
        if total is None:
            return f"{label}, {_files_phrase(processed)} so far"
        return f"{label}, {processed} of {total}"
    return f"{label}…"


def _synced_roots_parent_index(roots: list[SyncRootRecord]) -> list[Optional[int]]:
    """For each root in `roots`, the index of its nearest ancestor also
    present in `roots` (longest matching path prefix), or None if it's
    top-level. Only filesystem roots can nest - root_id is a resolved
    path only for that connector; a Drive folder id has no derivable
    containment relationship. Pure, so it's testable without Qt."""
    parent_index: list[Optional[int]] = [None] * len(roots)
    for i, row in enumerate(roots):
        if row.connector != "filesystem":
            continue
        best_len = -1
        for j, other in enumerate(roots):
            if i == j or other.connector != "filesystem":
                continue
            prefix = f"{other.root_id}{os.sep}"
            if row.root_id.startswith(prefix) and len(other.root_id) > best_len:
                parent_index[i] = j
                best_len = len(other.root_id)
    return parent_index


def _row_display_name(row: SyncRootRecord) -> str:
    """The folder basename for filesystem roots; for Drive, the folder
    name the last sync looked up, never the raw folder id. Pure, so it's
    testable without Qt."""
    if row.connector == "filesystem":
        return Path(row.root_id).name or row.root_id
    return row.display_name or _DRIVE_FOLDER_FALLBACK_NAME


def _with_nesting_depth(roots: list[SyncRootRecord]) -> list[tuple[SyncRootRecord, int]]:
    """`roots` (already ordered) flattened depth-first, each child right
    after its parent with its nesting depth - the Sources list is flat
    cards, nesting shows only as indent. Pure, so it's testable without
    Qt."""
    parent_index = _synced_roots_parent_index(roots)
    children: dict[Optional[int], list[int]] = {}
    for i, parent in enumerate(parent_index):
        children.setdefault(parent, []).append(i)
    flattened: list[tuple[SyncRootRecord, int]] = []

    def _walk(parent: Optional[int], depth: int) -> None:
        for i in children.get(parent, []):
            flattened.append((roots[i], depth))
            _walk(i, depth + 1)

    _walk(None, 0)
    return flattened


def _order_synced_roots(roots: list[SyncRootRecord], syncing_root_id: str) -> list[SyncRootRecord]:
    """DESIGN.md §4.6 ordering with "needs attention" omitted (no such
    signal is reachable from the client today - see the Phase 5 plan):
    the currently-syncing root first, everything else alphabetically by
    display name. Safe to apply before `_synced_roots_parent_index` -
    that function only compares relative root_id/connector relationships
    between entries, never absolute list position. Pure, so it's
    testable without Qt."""
    return sorted(roots, key=lambda r: (0 if r.root_id == syncing_root_id else 1, _row_display_name(r).lower()))


def _row_sync_state(root_id: str, syncing_root_id: str, is_polling: bool, is_cancelling: bool) -> str:
    """"needs_attention"/"not_synced_yet" are deliberately not states
    this ever returns - see the Phase 5 plan for why. Pure, so it's
    testable without Qt."""
    if not is_polling:
        return "synced"
    if root_id != syncing_root_id:
        return "busy"
    return "cancelling" if is_cancelling else "syncing"


class SyncedRootRow(QFrame):
    """One row in the Synced Folders list (DESIGN.md §4.6): name,
    secondary line, a status tag, one action - at least 48px tall (see
    `theme.py`'s `QFrame#sourceRow` rule). Built once per row in
    `_on_synced_roots_loaded`; `update_state()` mutates it afterward -
    a deliberate deviation from `setup_checklist.py`'s rows (which are
    rebuilt from scratch every time), because this one has to update on
    every ~750ms poll tick without triggering a new `GET /sync_roots`
    call or collapsing the tree's `expandAll()`/scroll state each time.

    "needs attention"/"not synced yet" are never states this renders -
    see the Phase 5 plan for why (no real per-root last-sync-failed
    signal is reachable from the client today; the "not synced yet" row
    never exists under the current architecture)."""

    def __init__(
        self, row: SyncRootRecord, on_sync_now, on_cancel, on_remove, depth: int = 0, parent: QWidget = None,
    ):
        super().__init__(parent)
        self.setFrameShape(QFrame.Shape.NoFrame)
        # Natural height always - inside the scroll area a list taller than
        # the window must scroll, not squeeze every row.
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.root_id = row.root_id
        self._row = row
        self._on_sync_now = on_sync_now
        self._on_cancel = on_cancel
        self._state = "synced"

        icon_tile = QFrame()
        icon_tile.setObjectName("sourceIconTile")
        icon_tile.setFixedSize(theme.SOURCE_ICON_TILE, theme.SOURCE_ICON_TILE)
        self._icon_label = icon_label = QLabel()
        icon_label.setPixmap(
            theme.icon_from_svg(
                theme.FOLDER_SVG if row.connector == "filesystem" else theme.CLOUD_SVG,
                theme.TEXT_SECONDARY,
            ).pixmap(20, 20)
        )
        icon_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        tile_layout = QHBoxLayout(icon_tile)
        tile_layout.setContentsMargins(0, 0, 0, 0)
        tile_layout.addWidget(icon_label)

        self._name_label = QLabel(_row_display_name(row))
        self._name_label.setObjectName("sourceRowName")
        self._tag = QLabel("Synced")
        self._tag.setObjectName("sourceRowTag")
        self._tag.setProperty("tagKind", "synced")
        name_row = QHBoxLayout()
        name_row.setContentsMargins(0, 0, 0, 0)
        name_row.setSpacing(theme.SPACING_SM)
        name_row.addWidget(self._name_label)
        name_row.addWidget(self._tag)
        name_row.addStretch(1)

        self._secondary_label = QLabel(self._idle_secondary_text())
        self._secondary_label.setObjectName("sourceRowSecondary")
        text_col = QVBoxLayout()
        text_col.setContentsMargins(0, 0, 0, 0)
        text_col.setSpacing(2)
        text_col.addLayout(name_row)
        text_col.addWidget(self._secondary_label)

        self._action_button = QPushButton("Sync now")
        self._action_button.clicked.connect(self._on_action_clicked)
        # Quiet, beside the row's one main action, and only while nothing
        # is syncing - the background service refuses a removal mid-sync.
        self._remove_button = QPushButton("Remove")
        self._remove_button.setProperty("buttonKind", "quiet")
        self._remove_button.clicked.connect(on_remove)
        self._remove_button.hide()

        self._progress_panel = QFrame()
        self._progress_panel.setObjectName("syncProgressPanel")
        self._progress_text = QLabel()
        self._progress_text.setObjectName("syncProgressText")
        self._progress_bar = QProgressBar()
        self._progress_bar.setObjectName("syncProgressBar")
        self._progress_bar.setTextVisible(False)
        self._progress_bar.setFixedHeight(6)
        self._progress_bar.setRange(0, 1000)
        progress_layout = QVBoxLayout(self._progress_panel)
        progress_layout.setSpacing(theme.SPACING_SM)
        progress_layout.addWidget(self._progress_text)
        progress_layout.addWidget(self._progress_bar)
        self._progress_panel.hide()

        # The card is an inner frame so the slot can carry the gap
        # between rows.
        card = QFrame()
        card.setObjectName("sourceRow")
        top_row = QHBoxLayout()
        top_row.setSpacing(theme.SPACING_MD)
        top_row.addWidget(icon_tile)
        top_row.addLayout(text_col, 1)
        top_row.addWidget(self._remove_button)
        top_row.addWidget(self._action_button)
        card_layout = QVBoxLayout(card)
        card_layout.setSpacing(theme.SPACING_MD)
        card_layout.addLayout(top_row)
        card_layout.addWidget(self._progress_panel)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(depth * theme.SPACING_XL, 0, 0, theme.SPACING_SM)
        layout.addWidget(card)

    def _idle_secondary_text(self) -> str:
        synced = format_utils.format_relative_time(self._row.last_synced_at)
        if self._row.connector == "filesystem":
            return f"{format_utils.tilde_path(self._row.root_id)} · {synced}"
        return f"Synced {synced}"

    def _on_action_clicked(self) -> None:
        (self._on_cancel if self._state in ("syncing", "cancelling") else self._on_sync_now)()

    def update_state(self, state: str, progress_text: str = "", fraction: Optional[float] = None) -> None:
        self._state = state
        syncing = state in ("syncing", "cancelling")
        if state == "cancelling":
            self._progress_text.setText("Cancelling. Jarvis will stop after this step. Everything already saved is kept.")
        elif state == "syncing":
            self._progress_text.setText(progress_text or "Starting…")
        if syncing:
            if fraction is None or state == "cancelling":
                self._progress_bar.setRange(0, 0)
            else:
                self._progress_bar.setRange(0, 1000)
                self._progress_bar.setValue(round(fraction * 1000))
        self._progress_panel.setVisible(syncing)
        self._secondary_label.setText(self._idle_secondary_text())
        self._tag.setText("Syncing" if syncing else "Synced")
        self._tag.setProperty("tagKind", "syncing" if syncing else "synced")
        self._action_button.setText("Cancel" if syncing else "Sync now")
        self._action_button.setEnabled(state not in ("busy", "cancelling"))
        self._action_button.setProperty("buttonKind", "quiet" if syncing else "")
        self._remove_button.setVisible(state == "synced")
        for widget in (self._tag, self._action_button):
            widget.style().unpolish(widget)
            widget.style().polish(widget)


class ConnectorsView(QWidget):
    # Emitted with the real ingest-stats dict once an ingest actually
    # succeeds (never on a daemon-side or daemon-unreachable failure) -
    # MainWindow uses this to reload the hierarchy view automatically,
    # without this view needing any reference to that one. See the
    # hierarchy view's own docstring for the gap this closes.
    ingest_succeeded = Signal(dict)
    # DESIGN.md §4.1's Sources nav-rail tag (Phase 4) - MainWindow wires
    # both straight to NavRail.set_syncing/set_needs_attention, with no
    # reference back from this view to the nav rail.
    sync_state_changed = Signal(bool)
    setup_attention_changed = Signal(bool)
    go_to_search_clicked = Signal()

    def __init__(self, pool: QThreadPool, parent: QWidget = None):
        super().__init__(parent)
        self._pool = pool

        self._page_header = page_header.PageHeader(
            "Sources", action=("Add a source", self._on_add_source_clicked),
        )
        self._add_source_button = self._page_header.action_button

        self._status_banner = DaemonStatusBanner(errors_dismissible=False)

        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(_POLL_INTERVAL_MS)
        self._poll_timer.timeout.connect(self._poll_ingest_status)

        self._source_name = ""
        self._sync_errors = 0
        self._last_ingest_call: Optional[tuple] = None
        self._is_retry = False
        # Sources row state (DESIGN.md §4.6) - which row (if any) is
        # mid-cancel, the latest progress text for whichever row is
        # syncing, and the live SyncedRootRow widgets keyed by root_id
        # so polling can update them in place without a tree rebuild.
        self._cancel_requested = False
        self._last_progress_text = ""
        self._last_progress_fraction: Optional[float] = None
        self._synced_root_rows: dict[str, SyncedRootRow] = {}

        # Setup checklist (DESIGN.md §4.7) - has_source is None ("Checking…")
        # until the first real GET /sync_roots result lands;
        # source_card_dismissed_for_session is "Not now" on the Add-a-
        # source step specifically, session-only (not persisted, unlike
        # the Claude Desktop dismiss) - the buttons row above still lets
        # a user add a folder any time regardless.
        self._has_source: Optional[bool] = None
        self._source_card_dismissed_for_session = False
        self._checklist_card = setup_checklist.SetupChecklistCard()
        # The slot takes the empty page's spare height below the card, so
        # neither the card nor the banner above it stretches to fill it.
        self._checklist_card_slot = QWidget()
        card_slot_layout = QVBoxLayout(self._checklist_card_slot)
        card_slot_layout.setContentsMargins(0, 0, 0, 0)
        card_slot_layout.addLayout(empty_state.centred_row(self._checklist_card))
        card_slot_layout.addStretch(1)
        self._show_checklist_card(False)
        self._checklist_strip = setup_checklist.SetupChecklistStrip()
        self._checklist_strip.hide()

        body_layout = QVBoxLayout()
        body_layout.setContentsMargins(
            theme.SPACING_XL, theme.SPACING_LG, theme.SPACING_XL, theme.SPACING_LG,
        )
        body_layout.setSpacing(theme.SPACING_LG)
        body_layout.addWidget(self._status_banner)
        body_layout.addWidget(self._checklist_card_slot, 1)
        body_layout.addWidget(self._checklist_strip)
        self._synced_roots_group = self._build_synced_roots_group()
        body_layout.addWidget(self._synced_roots_group, 1)

        # PageHeader spans edge-to-edge (Figma parity pass) - the outer
        # layout carries no margins of its own; body_layout's own
        # margins above are the page padding (DESIGN.md §2.4) instead.
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self._page_header)
        layout.addLayout(body_layout, 1)

        self.ingest_succeeded.connect(lambda _result: self._load_synced_roots())

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._load_synced_roots()

    def _build_synced_roots_group(self) -> QWidget:
        group = QWidget()
        title = QLabel("Your sources")
        title.setObjectName("sourcesListTitle")
        self._count_pill = QLabel("0 total")
        self._count_pill.setObjectName("sourcesCountPill")
        header = QHBoxLayout()
        header.setSpacing(theme.SPACING_SM)
        header.addWidget(title)
        header.addWidget(self._count_pill)
        header.addStretch(1)

        self._synced_roots_list = QWidget()
        self._synced_roots_list.setObjectName("sourcesList")
        self._synced_roots_rows_layout = QVBoxLayout(self._synced_roots_list)
        self._synced_roots_rows_layout.setContentsMargins(0, 0, 0, 0)
        self._synced_roots_rows_layout.setSpacing(0)
        self._synced_roots_rows_layout.addStretch(1)
        scroll = QScrollArea()
        scroll.setObjectName("sourcesScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setWidget(self._synced_roots_list)

        group_layout = QVBoxLayout(group)
        group_layout.setContentsMargins(0, 0, 0, 0)
        group_layout.setSpacing(theme.SPACING_MD)
        group_layout.addLayout(header)
        group_layout.addWidget(scroll, 1)
        return group

    def _load_synced_roots(self) -> None:
        run_daemon_call(self._pool, daemon_client.get_sync_roots, on_success=self._on_synced_roots_loaded)

    def _on_synced_roots_loaded(self, result) -> None:
        if isinstance(result, ErrorResponse):
            self._status_banner.show_error(result.error.message)
            return
        assert isinstance(result, SyncRootsResult)

        for row_widget in self._synced_root_rows.values():
            self._synced_roots_rows_layout.removeWidget(row_widget)
            row_widget.deleteLater()
        syncing_root_id = self._source_name if self._poll_timer.isActive() else ""
        roots = _order_synced_roots(result.roots, syncing_root_id)
        self._synced_root_rows = {}
        for position, (row, depth) in enumerate(_with_nesting_depth(roots)):
            row_widget = SyncedRootRow(
                row, on_sync_now=self._make_sync_now(row), on_cancel=self._on_cancel_clicked,
                on_remove=lambda _checked=False, row=row: self._on_remove_clicked(row), depth=depth,
            )
            self._synced_roots_rows_layout.insertWidget(position, row_widget)
            self._synced_root_rows[row.root_id] = row_widget
        self._refresh_row_states()
        self._count_pill.setText(f"{len(roots)} total")

        self._has_source = len(roots) > 0
        self.refresh_setup_checklist()

    def refresh_setup_checklist(self) -> None:
        """The one variant-selection decision point (DESIGN.md §4.7):
        all_done -> neither widget, the real tree shows; zero/unknown
        sources (and not session-dismissed) -> the full card only (an
        empty tree AND a full empty-state card would double up, so the
        tree group hides); otherwise -> the compact strip only, tree
        group visible. Called after anything that could change the
        result - a real sync-roots load, an ingest finishing, the Claude
        Desktop dialog closing, or one of this checklist's own actions -
        there's no change-notification to subscribe to (session_store
        has none either), so every caller just re-checks and re-renders,
        the same pattern settings_view.py's refresh_account_label()
        already uses."""
        # Without a cloud server there's nothing to sign in to, so the step
        # counts as done and its row stays hidden.
        signed_in = not cloud_client.is_configured() or session_store.load_session() is not None
        claude_plan = cds.build_plan()
        claude_dismissed = setup_checklist.is_claude_desktop_dismissed()
        health = setup_health.compute_setup_health(
            signed_in=signed_in, has_source=self._has_source,
            claude_plan=claude_plan, claude_dismissed=claude_dismissed,
        )
        # DESIGN.md §4.1's "Setup-health regressions" - not just "not
        # yet done" (has_source=False on a fresh install isn't a
        # regression, it's normal onboarding; the Claude Desktop step
        # being merely unconnected isn't either, since it's optional).
        # Genuinely broken/degraded states only.
        self.setup_attention_changed.emit(
            not health.signed_in or health.claude_desktop in ("needs_repair", "translocated")
        )
        items = self._build_checklist_items(health)

        if health.all_done:
            self._show_checklist_card(False)
            self._checklist_strip.hide()
            self._synced_roots_group.show()
        elif self._has_source is not True and not self._source_card_dismissed_for_session:
            self._checklist_card.update_items(items)
            self._show_checklist_card(True)
            self._checklist_strip.hide()
            self._synced_roots_group.hide()
        else:
            self._show_checklist_card(False)
            self._checklist_strip.update_items(items, health.steps_done, health.total_steps)
            self._checklist_strip.show()
            self._synced_roots_group.show()

    def _show_checklist_card(self, visible: bool) -> None:
        """Once the card offers its own "Add a source" (no sources yet),
        that is the page's one primary action (DESIGN.md §4.3) and the
        header's hides. While sources are still unknown - including after
        a failed load - the card has no such button, so the header's stays."""
        self._checklist_card.setVisible(visible)
        self._checklist_card_slot.setVisible(visible)
        self._add_source_button.setVisible(not (visible and self._has_source is False))

    # DESIGN.md §6 (Setup) gives Claude Desktop a different sentence per
    # real state, not one static label with only the action button
    # changing - `not_connected` also covers the "isn't installed" case
    # setup_health.py deliberately folds in (no reliable signal for it).
    _CLAUDE_DESKTOP_MESSAGES = {
        "connected": "Connect Claude Desktop (optional).",
        "not_connected": "Connect Claude Desktop (optional).",
        "dismissed": "Connect Claude Desktop (optional).",
        "needs_repair": "The Claude connection needs repair.",
        "translocated": "Move Jarvis to your Applications folder and reopen it.",
    }

    def _build_checklist_items(self, health: setup_health.SetupHealth) -> list[setup_checklist.ChecklistItem]:
        signed_in_state = (
            setup_health.signed_in_item_state(health) if cloud_client.is_configured() else "dismissed"
        )
        source_state = setup_health.source_item_state(health)
        claude_state = setup_health.claude_desktop_item_state(health)

        return [
            setup_checklist.ChecklistItem(
                "Signed in", signed_in_state,
                strip_text="Sign in to keep using Jarvis." if signed_in_state != "done" else None,
                primary_action=None if signed_in_state == "done" else ("Quit Jarvis", self._on_signed_in_quit_clicked),
            ),
            setup_checklist.ChecklistItem(
                "Add your first source", source_state,
                message=(
                    "Point Jarvis at a folder or Google Drive. It reads the files once, "
                    "then keeps them current."
                ) if source_state == "to_do" else None,
                primary_action=("Add a source", self._on_add_source_clicked) if source_state == "to_do" else None,
                secondary_action=("Not now", self._on_source_not_now_clicked) if source_state == "to_do" else None,
                strip_text="Add a source so Claude can use your files." if source_state == "to_do" else None,
                emphasized=True,
            ),
            setup_checklist.ChecklistItem(
                self._CLAUDE_DESKTOP_MESSAGES[health.claude_desktop], claude_state,
                primary_action=(
                    ("Repair" if claude_state == "needs_attention" else "Connect", self._on_claude_desktop_action_clicked)
                    if claude_state in ("to_do", "needs_attention") else None
                ),
                secondary_action=(
                    ("I don't use Claude Desktop", self._on_dismiss_claude_desktop_clicked)
                    if claude_state in ("to_do", "needs_attention") else None
                ),
                strip_text=(
                    "Connect Claude Desktop so Claude can use your files." if claude_state == "to_do" else None
                ),
            ),
        ]

    def _on_signed_in_quit_clicked(self) -> None:
        SignInDialog(self, self._pool, first_run=True).exec()
        self.refresh_setup_checklist()

    def _on_source_not_now_clicked(self) -> None:
        self._source_card_dismissed_for_session = True
        self.refresh_setup_checklist()

    def _on_claude_desktop_action_clicked(self) -> None:
        ConnectClaudeDesktopDialog(self).exec()
        self.refresh_setup_checklist()

    def _on_dismiss_claude_desktop_clicked(self) -> None:
        setup_checklist.set_claude_desktop_dismissed(True)
        self.refresh_setup_checklist()

    def _make_sync_now(self, row: SyncRootRecord):
        ingest_fn = (
            daemon_client.run_ingest_filesystem
            if row.connector == "filesystem"
            else daemon_client.run_ingest_drive
        )
        return lambda: self._start_ingest(row.root_id, ingest_fn, row.root_id)

    def _on_remove_clicked(self, row: SyncRootRecord) -> None:
        # DESIGN.md §4.10: the title states the action, the body states the
        # consequence, and focus starts on Cancel.
        if confirm_action(
            self, f"Remove “{_row_display_name(row)}”?",
            "Its files will no longer appear in search. Your files themselves aren't changed.",
            "Remove", destructive=True,
        ):
            self._remove_source(row)

    def _remove_source(self, row: SyncRootRecord) -> None:
        name = _row_display_name(row)
        self._set_controls_enabled(False)
        self._synced_roots_list.setEnabled(False)
        self._status_banner.show_info(f"Removing “{name}”…")
        run_daemon_call(
            self._pool, daemon_client.remove_sync_root, row.connector, row.root_id,
            on_success=lambda result: self._on_remove_result(row, result),
            on_error=lambda exc: self._on_remove_error(row, exc),
        )

    def _on_remove_result(self, row: SyncRootRecord, result: dict) -> None:
        self._set_controls_enabled(True)
        self._synced_roots_list.setEnabled(True)
        if "error" in result:
            def retry() -> None:
                self._remove_source(row)

            action = (
                ("Reconnect Drive", lambda: self._reconnect_drive_then(retry))
                if result["error"]["code"] == "drive_auth_expired"
                else ("Try again", retry)
            )
            self._status_banner.show_error(result["error"]["message"], action=action)
            return
        self._status_banner.show_success(
            f"Removed “{_row_display_name(row)}”. Its files no longer appear in search.",
        )
        self._load_synced_roots()

    def _on_remove_error(self, row: SyncRootRecord, exc: Exception) -> None:
        self._set_controls_enabled(True)
        self._synced_roots_list.setEnabled(True)
        message = _SERVICE_UNREACHABLE_MESSAGE if isinstance(exc, DaemonError) else _UNKNOWN_MESSAGE
        self._status_banner.show_error(message, action=("Try again", lambda: self._remove_source(row)))

    def _refresh_row_states(self) -> None:
        is_polling = self._poll_timer.isActive()
        for root_id, row_widget in self._synced_root_rows.items():
            state = _row_sync_state(root_id, self._source_name, is_polling, self._cancel_requested)
            row_widget.update_state(
                state,
                self._last_progress_text if state == "syncing" else "",
                self._last_progress_fraction if state == "syncing" else None,
            )

    def _set_controls_enabled(self, enabled: bool) -> None:
        # Not the synced-roots tree - DESIGN.md §4.6's "busy" state keeps
        # the actively-syncing row's own Cancel action clickable while
        # every other row disables, which a tree-wide setEnabled(False)
        # would silently defeat (Qt disables every descendant of a
        # disabled parent regardless of a child's own setEnabled(True)
        # call). The tree's per-row enabled state is governed entirely by
        # _refresh_row_states() now; the Drive-OAuth-wait window (where
        # no row distinction applies) disables it directly instead - see
        # _on_connect_drive/_on_drive_connected/_on_drive_connect_failed.
        self._add_source_button.setEnabled(enabled)
        # The card's own "Add a source" too, so a second sync can't start
        # alongside the first. On the card, not its rows: they're rebuilt.
        self._checklist_card.setEnabled(enabled)

    def _on_add_source_clicked(self) -> None:
        dialog = AddSourceDialog(self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        if dialog.chosen() == add_source_dialog.FOLDER:
            self._on_add_folder()
        else:
            self._on_connect_drive()

    def _on_add_folder(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Select a folder to sync")
        if not path:
            return
        self._start_ingest(Path(path).name or path, daemon_client.run_ingest_filesystem, path)

    def _start_ingest(self, source_name: str, ingest_fn, *args, is_retry: bool = False) -> None:
        self._last_ingest_call = (source_name, ingest_fn, args)
        self._is_retry = is_retry
        self.sync_state_changed.emit(True)
        self._set_controls_enabled(False)
        self._source_name = source_name
        self._sync_errors = 0
        self._cancel_requested = False
        self._last_progress_text = ""
        self._last_progress_fraction = None
        self._status_banner.show_info(self._ingesting_message())
        self._poll_timer.start()
        self._refresh_row_states()
        run_daemon_call(
            self._pool, ingest_fn, *args,
            on_success=self._on_ingest_result,
            on_error=self._on_ingest_error,
        )

    def _on_connect_drive(self) -> None:
        self._set_controls_enabled(False)
        self._synced_roots_list.setEnabled(False)
        self._status_banner.show_info("Sign in to Google to continue.")
        run_daemon_call(
            self._pool, drive.get_drive_service, allow_interactive_auth=True,
            on_success=self._on_drive_connected,
            on_error=self._on_drive_connect_failed,
        )

    def _on_drive_connected(self, service) -> None:
        self._set_controls_enabled(True)
        self._synced_roots_list.setEnabled(True)
        self._status_banner.dismiss()
        dialog = DriveFolderDialog(service, self._pool, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            folder_id, folder_name = dialog.selected_folder()
            self._start_ingest(folder_name, daemon_client.run_ingest_drive, folder_id)

    def _on_drive_connect_failed(self, exc: Exception) -> None:
        # Never a DaemonError - get_drive_service() never touches the
        # daemon (module docstring); this is a RuntimeError (missing
        # client id/secret, a non-interactive/expired-token message) or
        # a real Google API/OAuth failure.
        self._set_controls_enabled(True)
        self._synced_roots_list.setEnabled(True)
        error_reporting.report_error(self._pool, "drive_connect_failed", str(exc))
        self._status_banner.show_error(
            "Google Drive isn't connected. Nothing was changed.",
            action=("Try again", self._on_connect_drive),
        )

    def _on_cancel_clicked(self) -> None:
        # Disabled immediately, not just once the daemon confirms - a
        # cancel request is a single flag flip (server.py), so a second
        # click before the first response lands has nothing further to
        # do; re-enabled only if the request itself fails to reach the
        # daemon (_on_cancel_request_failed), so the user can retry.
        self._cancel_requested = True
        self._refresh_row_states()
        run_daemon_call(
            self._pool, daemon_client.cancel_ingest,
            on_success=self._on_cancel_requested, on_error=self._on_cancel_request_failed,
        )

    def _on_cancel_requested(self, response: dict) -> None:
        if response.get("cancel_requested"):
            # Set immediately rather than waiting for the next poll tick -
            # cooperative cancellation can take a while (a whole embed
            # batch, or longer), and the user just asked for it.
            self._cancel_requested = True
            self._refresh_row_states()
        else:
            # The ingest this click was aimed at already finished on its
            # own between this click and the daemon processing it - not
            # an error, just a harmless race; _on_ingest_result/_on_ingest_
            # error has already re-enabled everything by now.
            self._cancel_requested = False
            self._refresh_row_states()

    def _on_cancel_request_failed(self, exc: Exception) -> None:
        self._cancel_requested = False
        self._refresh_row_states()
        self._status_banner.show_error(
            "The sync is still running. Jarvis couldn't stop it.", action=("Try again", self._on_cancel_clicked),
        )

    def _poll_ingest_status(self) -> None:
        run_daemon_call(
            self._pool, daemon_client.get_ingest_status,
            on_success=self._on_status_polled, on_error=self._on_status_poll_failed,
        )

    def _on_status_polled(self, status: dict) -> None:
        if not self._poll_timer.isActive():
            return  # the main ingest call already resolved - see module docstring
        phase = status.get("phase")
        progress = status.get("progress")
        if phase == pipeline_mod.PHASE_READING_FILES and progress and progress["errors"] != self._sync_errors:
            self._sync_errors = progress["errors"]
            self._status_banner.show_info(self._ingesting_message())

        if status.get("cancel_requested"):
            self._cancel_requested = True
        elif phase:
            self._last_progress_text = format_phase_progress(phase, progress)
            self._last_progress_fraction = _progress_fraction(phase, progress)
        self._refresh_row_states()

    def _ingesting_message(self) -> str:
        verb = "Retrying" if self._is_retry else "Syncing"
        message = f"{verb} “{self._source_name}”. You can keep using Jarvis."
        if self._sync_errors:
            message += f" ({_files_phrase(self._sync_errors)} couldn't be read)"
        return message

    def _retry_last_ingest(self) -> None:
        if self._last_ingest_call is None:
            return
        source_name, ingest_fn, args = self._last_ingest_call
        self._start_ingest(source_name, ingest_fn, *args, is_retry=True)

    def _on_status_poll_failed(self, exc: Exception) -> None:
        # One missed poll tick is expected and harmless - a transient
        # daemon hiccup (or two, given daemon_client.call()'s own one
        # retry) already gets logged there; the next tick, _POLL_INTERVAL_MS
        # later, just tries again. Never surfaced to the user: the
        # ingest's own on_success/on_error (not this) is what determines
        # success or failure. Explicit, not the implicit "no on_error
        # means run_daemon_call connects nothing" default - so a reader
        # doesn't have to trace into daemon_worker.py to confirm a
        # failed poll can't crash or block anything.
        pass

    def _stop_polling(self) -> None:
        self._poll_timer.stop()
        self._set_controls_enabled(True)
        self._cancel_requested = False
        self._refresh_row_states()
        self.sync_state_changed.emit(False)

    def _on_ingest_result(self, result: dict) -> None:
        self._stop_polling()
        if "error" in result:
            self._show_sync_failure(result["error"]["code"], result["error"]["message"])
            return
        if result.get("cancelled"):
            pulled = result.get("partial", {}).get("sync", {}).get("pulled", 0)
            self._status_banner.show_info(
                f"Stopped. {_files_phrase(pulled)} are saved. Retry picks up where it left off.",
                action=("Retry", self._retry_last_ingest),
            )
            return
        errors = result["sync"]["errors"]
        if errors:
            pulled = result["sync"]["pulled"]
            self._status_banner.show_error(
                f"Mostly done. {_files_phrase(errors)} couldn't be read. The other "
                f"{_files_phrase(pulled)} are saved.",
                action=("Retry", self._retry_last_ingest),
            )
        else:
            self._status_banner.show_success(
                f"All caught up. {_files_phrase(result['sync']['pulled'])} saved from "
                f"“{self._source_name}”. Nothing needs your attention.",
                action=("Search your files", self.go_to_search_clicked.emit),
            )
        self.ingest_succeeded.emit(result)

    def _on_ingest_error(self, exc: Exception) -> None:
        self._stop_polling()
        if isinstance(exc, DaemonError):
            self._status_banner.show_error(
                _SERVICE_UNREACHABLE_MESSAGE, action=("Retry", self._retry_last_ingest),
            )
        else:
            self._show_sync_failure(_UNKNOWN_CODE, _UNKNOWN_MESSAGE)

    def _show_sync_failure(self, code: str, message: str) -> None:
        """A failed sync as an error banner with the one action that fixes
        it (DESIGN.md §4.2, §5.4: never a modal, never raw error text).
        `message` is the background service's own calm text for `code`."""
        action = {
            "embedding_key_invalid": ("Fix API key", self._fix_api_key_and_retry),
            "drive_auth_expired": ("Reconnect Drive", self._reconnect_drive_and_retry),
        }.get(code, ("Retry", self._retry_last_ingest))
        secondary_action = (
            ("Send feedback", self._open_feedback)
            if code == _UNKNOWN_CODE and cloud_client.is_configured() else None
        )
        self._status_banner.show_error(message, action=action, secondary_action=secondary_action)

    def _fix_api_key_and_retry(self) -> None:
        # A new key only reaches the background service when it restarts,
        # so saving, restarting and retrying are one action (ticket 05).
        if SettingsDialog(self).exec() != QDialog.DialogCode.Accepted:
            return
        self._status_banner.show_info("Restarting…")
        run_daemon_call(
            self._pool, process_stats.force_restart_daemon,
            on_success=lambda _result: self._retry_last_ingest(), on_error=self._on_ingest_error,
        )

    def _reconnect_drive_and_retry(self) -> None:
        self._reconnect_drive_then(self._retry_last_ingest)

    def _reconnect_drive_then(self, then) -> None:
        self._status_banner.show_info("Sign in to Google to continue.")
        run_daemon_call(
            self._pool, drive.get_drive_service, allow_interactive_auth=True,
            on_success=lambda _service: then(),
            on_error=self._on_drive_connect_failed,
        )

    def _open_feedback(self) -> None:
        FeedbackDialog(self, self._pool).exec()
