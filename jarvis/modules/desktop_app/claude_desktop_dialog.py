"""Dialog for connecting a packaged Jarvis build to Claude Desktop as an
MCP server. All the generate-and-decide logic lives in `jarvis.modules.
desktop_app.claude_desktop_setup` (`build_plan()` read-only, `apply_plan()`
the one write path) - this file is only the display of that plan plus
the two user-initiated actions on it, opened from the same "&Settings"
menu `SettingsDialog` already lives on (see `MainWindow`): an occasional
setup screen, not daemon-dependent content like the three tabs, the
same category as configuring the embedding API key.

**"Connect automatically" is the primary, recommended action; copy-paste
stays as a manual fallback behind "Set up by hand", not removed.** The
apply button only appears when `plan.full_config is not None` (i.e.
there's genuinely something safe to write - see that module's own
docstring for which cases qualify) and always asks for confirmation
first, naming the exact file path and mentioning the backup, before
`claude_desktop_setup.apply_plan()` ever touches disk - the same
"explicit action, explicit confirmation" shape the Drive folder
picker's own ingest confirmation already uses, not a new pattern.

**Fragment vs. whole file is still a safety distinction for the manual
path, not a wording preference.** `claude_desktop_setup.build_plan()`'s
`CASE_MISSING` is the only case where copying the snippet over the
*entire* file is correct - every other case (`CASE_NO_MCP_SERVERS`,
`CASE_HAS_MCP_SERVERS`) hands back a fragment meant to be merged into a
file that may already carry a lot of unrelated content (confirmed
directly: a real installed Claude Desktop on this machine had ~60 lines
of unrelated preferences alongside `mcpServers`). The copy button's own
label switches between "file contents" and "fragment" for exactly this
reason, and the fragment cases get an extra, explicit "do NOT replace
the whole file" line above the text box - a careless select-all-and-paste
habit would otherwise destroy whatever else is in there. None of this
changes for the automatic path, since `apply_plan()` writes the whole,
correctly-merged file itself - the fragment/whole-file distinction is
purely a hazard of pasting by hand.
"""

import os
import subprocess
import sys

from PySide6.QtGui import QFontDatabase
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from jarvis.modules.desktop_app import claude_desktop_setup as cds
from jarvis.modules.desktop_app import theme

_FRAGMENT_CASES = {cds.CASE_NO_MCP_SERVERS, cds.CASE_HAS_MCP_SERVERS}


class ConnectClaudeDesktopDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Connect Claude Desktop")
        self.setModal(True)

        self._plan = cds.build_plan()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(theme.SPACING_XL, theme.SPACING_XL, theme.SPACING_XL, theme.SPACING_LG)
        layout.setSpacing(theme.SPACING_MD)
        lead = QLabel("Connect Jarvis to Claude Desktop, so Claude can use your files.")
        lead.setWordWrap(True)
        layout.addWidget(lead)

        # The by-hand steps (the file, the JSON to paste) live in their own
        # section. When Jarvis can make the change itself, that section
        # starts folded away behind "Set up by hand" - otherwise it is the
        # only way, so it shows straight away.
        self._manual_section = QWidget()
        manual_layout = QVBoxLayout(self._manual_section)
        manual_layout.setContentsMargins(0, 0, 0, 0)
        manual_layout.setSpacing(theme.SPACING_SM)

        self._apply_button = None
        self._manual_toggle = None
        if self._plan.full_config is not None:
            helper = QLabel("Jarvis adds itself to Claude Desktop's settings. Everything else there stays as it is.")
            helper.setWordWrap(True)
            helper.setObjectName("dialogHelperText")
            layout.addWidget(helper)

            self._apply_button = QPushButton("Connect automatically")
            self._apply_button.setProperty("buttonKind", "primary")
            self._apply_button.clicked.connect(self._on_apply)
            self._manual_toggle = QPushButton("Set up by hand")
            self._manual_toggle.setProperty("buttonKind", "quiet")
            self._manual_toggle.setCheckable(True)
            self._manual_toggle.toggled.connect(self._on_manual_toggled)
            actions = QHBoxLayout()
            actions.addWidget(self._apply_button)
            actions.addWidget(self._manual_toggle)
            actions.addStretch(1)
            layout.addLayout(actions)
            self._manual_section.hide()

        instructions = QLabel(self._plan.instructions)
        instructions.setWordWrap(True)
        manual_layout.addWidget(instructions)

        if self._plan.config_path is not None:
            path_field = QLineEdit(str(self._plan.config_path))
            path_field.setReadOnly(True)
            path_field.setCursorPosition(0)
            manual_layout.addWidget(path_field)

        self._copy_button = None
        if self._plan.snippet is not None:
            if self._plan.case in _FRAGMENT_CASES:
                warning = QLabel(
                    "This is a fragment to insert into your existing file - "
                    "do NOT replace the whole file with just this."
                )
                warning.setWordWrap(True)
                manual_layout.addWidget(warning)
                copy_label = "Copy fragment to clipboard"
            else:
                copy_label = "Copy file contents to clipboard"

            # Plain text in a fixed-width font: a QTextEdit given a string
            # reads it as rich text and folds the JSON onto one line.
            snippet_field = QPlainTextEdit()
            snippet_field.setPlainText(self._plan.snippet)
            snippet_field.setReadOnly(True)
            snippet_field.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
            snippet_field.setMinimumHeight(140)
            manual_layout.addWidget(snippet_field, 1)

            self._copy_button = QPushButton(copy_label)
            self._copy_button.clicked.connect(self._on_copy)

        self._open_folder_button = QPushButton("Open containing folder")
        self._open_folder_button.setEnabled(self._plan.config_path is not None)
        self._open_folder_button.clicked.connect(self._on_open_folder)

        manual_actions = QHBoxLayout()
        if self._copy_button is not None:
            manual_actions.addWidget(self._copy_button)
        manual_actions.addWidget(self._open_folder_button)
        manual_actions.addStretch(1)
        manual_layout.addLayout(manual_actions)

        layout.addWidget(self._manual_section, 1)
        # Takes any spare height while the section is folded away, so it
        # doesn't spread out the lines above.
        layout.addStretch(1)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self._fit_to_contents()

    def _on_manual_toggled(self, shown: bool) -> None:
        self._manual_section.setVisible(shown)
        self._fit_to_contents()

    def _fit_to_contents(self) -> None:
        self.layout().activate()
        self.resize(560, self.sizeHint().height())

    def _on_apply(self) -> None:
        config_path = self._plan.config_path
        backup_note = (
            f"\n\nYour current file will be backed up first, to:\n{config_path.name}.bak"
            if config_path.exists() else ""
        )
        confirmed = QMessageBox.question(
            self, "Update Claude Desktop's config?",
            f"This will update:\n{config_path}\n\n"
            f"to connect Jarvis to Claude Desktop. Everything else already in "
            f"that file - other servers, preferences - is preserved.{backup_note}\n\n"
            "Restart Claude Desktop to finish.",
        )
        if confirmed != QMessageBox.StandardButton.Yes:
            return

        try:
            backup_path = cds.apply_plan(self._plan)
        except OSError as exc:
            QMessageBox.critical(
                self, "Couldn't update the config file",
                f"{exc}\n\nNothing was changed. You can still add the connection "
                "manually using the fragment below.",
            )
            return

        message = "Claude Desktop's config has been updated."
        if backup_path is not None:
            message += f"\n\nYour previous file was backed up to:\n{backup_path}"
        message += "\n\nRestart Claude Desktop to finish."
        QMessageBox.information(self, "Done", message)
        self.accept()

    def _on_copy(self) -> None:
        QApplication.clipboard().setText(self._plan.snippet)

    def _on_open_folder(self) -> None:
        folder = self._plan.config_path.parent
        folder.mkdir(parents=True, exist_ok=True)
        # os.startfile is Windows-only (doesn't exist at all on macOS);
        # `open` is the standard macOS Finder-reveal idiom, no stdlib
        # equivalent exists. default_config_path() only ever returns
        # non-None on win32/darwin, so this covers every platform this
        # dialog can actually be reached on.
        if sys.platform == "darwin":
            subprocess.run(["open", str(folder)])
        else:
            os.startfile(str(folder))
