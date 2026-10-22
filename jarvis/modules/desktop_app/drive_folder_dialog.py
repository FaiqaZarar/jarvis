"""Google Drive folder-browser dialog
(docs/desktop-app-drive-connector-DESIGN.md §3b) - the Qt-native
replacement for `drive_ingest.py`'s `pick_folder()` `input()`/`print()`
loop, built on the same lazy-load-on-expand pattern `HierarchyView`
already uses for topics. Every call here goes through the same
`run_daemon_call` threading helper every other potentially-slow call in
this app already uses, but straight against
`jarvis_server.connectors.drive`, not `daemon_client` - Drive listing
stays client-side for the same reason OAuth does (see that design
doc's §1a, and `drive_ingest.py`'s own module docstring).

**Every folder node gets a "Loading…" placeholder child unconditionally**,
unlike `HierarchyView`'s topics (which know `subtree_chunk_count` up
front from `get_hierarchy()`'s response, so only genuinely non-empty
nodes get a placeholder). `list_drive_folders()` gives no such hint -
every node is assumed expandable until its real children come back
empty, at which point `_on_folders_loaded` removes the placeholder by
simply not re-adding one, which is also what correctly removes Qt's
expand arrow.

**The recursive preview scan is not a precondition for "Select this
folder"** (design doc §3b) - it runs once, at confirmation time, after
"Select this folder" is clicked, not while browsing. A scan failure
(anything other than a rate limit) doesn't block ingesting the folder
either, only the item-count summary in the confirmation text - the scan
is a nice-to-have, not a gate.
"""

from typing import Optional

from PySide6.QtCore import Qt, QThreadPool
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from jarvis.modules.desktop_app.daemon_worker import run_daemon_call
from jarvis.modules.desktop_app.error_presentation import confirm_action
from jarvis_server.connectors import drive

_FOLDER_ID_ROLE = Qt.ItemDataRole.UserRole
_ROOT_ID = "root"
_ROOT_LABEL = "My Drive"


def _rate_limit_message(exc: Exception) -> Optional[str]:
    """`drive.is_rate_limited` is safe to call on any exception (plain
    `getattr` checks, no `isinstance` gate) - no need to check the
    exception type ourselves first."""
    if drive.is_rate_limited(exc):
        return "Google is rate-limiting Drive requests right now. Wait a moment and try again."
    return None


class DriveFolderDialog(QDialog):
    def __init__(self, service, pool: QThreadPool, parent: QWidget = None):
        super().__init__(parent)
        self.setWindowTitle("Select a Google Drive folder")
        self.resize(480, 420)
        self._service = service
        self._pool = pool
        self._loaded_folder_ids: set[str] = set()
        self._pending_selection: Optional[tuple[str, str]] = None
        self._selected: Optional[tuple[str, str]] = None

        self._breadcrumb = QLabel(_ROOT_LABEL)
        self._breadcrumb.setWordWrap(True)

        self._tree = QTreeWidget()
        self._tree.setHeaderHidden(True)
        self._tree.itemExpanded.connect(self._on_item_expanded)
        self._tree.itemClicked.connect(self._on_item_clicked)

        root_item = QTreeWidgetItem([_ROOT_LABEL])
        root_item.setData(0, _FOLDER_ID_ROLE, _ROOT_ID)
        root_item.addChild(QTreeWidgetItem(["Loading…"]))
        self._tree.addTopLevelItem(root_item)

        self._select_button = QPushButton("Select this folder")
        self._select_button.setEnabled(False)
        self._select_button.clicked.connect(self._on_select_clicked)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        buttons.rejected.connect(self.reject)

        button_row = QHBoxLayout()
        button_row.addWidget(self._select_button)
        button_row.addStretch(1)
        button_row.addWidget(buttons)

        layout = QVBoxLayout(self)
        layout.addWidget(self._breadcrumb)
        layout.addWidget(self._tree, 1)
        layout.addLayout(button_row)

    def selected_folder(self) -> tuple[str, str]:
        """`(folder_id, folder_name)` - only meaningful once `exec()`
        has returned `Accepted`."""
        assert self._selected is not None
        return self._selected

    def _on_item_expanded(self, item: QTreeWidgetItem) -> None:
        folder_id = item.data(0, _FOLDER_ID_ROLE)
        if folder_id is None or folder_id in self._loaded_folder_ids:
            return
        self._loaded_folder_ids.add(folder_id)
        run_daemon_call(
            self._pool, drive.list_drive_folders, self._service, parent_id=folder_id,
            on_success=lambda folders, item=item: self._on_folders_loaded(item, folders),
            on_error=lambda exc, item=item: self._on_listing_failed(item, exc),
        )

    def _on_folders_loaded(self, item: QTreeWidgetItem, folders: list) -> None:
        item.takeChildren()
        for folder in folders:
            child = QTreeWidgetItem([folder["name"]])
            child.setData(0, _FOLDER_ID_ROLE, folder["id"])
            child.addChild(QTreeWidgetItem(["Loading…"]))
            item.addChild(child)

    def _on_listing_failed(self, item: QTreeWidgetItem, exc: Exception) -> None:
        item.takeChildren()
        self._loaded_folder_ids.discard(item.data(0, _FOLDER_ID_ROLE))
        message = _rate_limit_message(exc) or f"Couldn't list this folder's contents: {exc}"
        QMessageBox.critical(self, "Google Drive", message)

    def _on_item_clicked(self, item: QTreeWidgetItem, _column: int) -> None:
        folder_id = item.data(0, _FOLDER_ID_ROLE)
        if folder_id is None:
            return
        self._breadcrumb.setText(self._path_to(item))
        self._pending_selection = (folder_id, item.text(0))
        self._select_button.setEnabled(True)

    def _path_to(self, item: QTreeWidgetItem) -> str:
        names = [item.text(0)]
        parent = item.parent()
        while parent is not None:
            names.append(parent.text(0))
            parent = parent.parent()
        return " / ".join(reversed(names))

    def _on_select_clicked(self) -> None:
        if self._pending_selection is None:
            return
        folder_id, folder_name = self._pending_selection
        self._select_button.setEnabled(False)
        run_daemon_call(
            self._pool, drive.list_drive_files_recursive, self._service, folder_id,
            on_success=lambda files: self._confirm_ingest(folder_id, folder_name, files),
            on_error=lambda exc: self._on_preview_scan_failed(folder_id, folder_name, exc),
        )

    def _confirm_ingest(self, folder_id: str, folder_name: str, files: Optional[list] = None) -> None:
        self._select_button.setEnabled(True)
        detail = ""
        if files is not None:
            ingestable = sum(1 for f in files if f.get("mimeType") in drive.INGESTABLE_MIMETYPES)
            detail = (
                f"\n\n{len(files)} item(s) found, {ingestable} directly sync-able "
                "(Google Docs, Word, PDF)."
            )
        confirmed = confirm_action(
            self, f'Sync "{folder_name}"?',
            f"This downloads and indexes its files.{detail}",
            "Sync",
        )
        if confirmed:
            self._selected = (folder_id, folder_name)
            self.accept()

    def _on_preview_scan_failed(self, folder_id: str, folder_name: str, exc: Exception) -> None:
        message = _rate_limit_message(exc)
        if message is not None:
            self._select_button.setEnabled(True)
            QMessageBox.critical(self, "Google Drive", message)
            return
        # Not a rate limit - the preview is a nice-to-have confirmation,
        # not a precondition (module docstring), so a scan failure
        # doesn't block ingesting the folder, only the summary text.
        self._confirm_ingest(folder_id, folder_name, files=None)
