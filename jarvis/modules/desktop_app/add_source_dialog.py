"""The Add-source chooser (DESIGN.md §4.10): pick Folder or Google
Drive, replacing `connectors_view.py`'s old two-separate-buttons entry
point with one "Add a source" button that opens this instead. Each
choice dispatches to the exact same, unchanged flow those two buttons
already triggered (`ConnectorsView._on_add_folder`/`_on_connect_drive`)
- this dialog only decides *which* of the two to call, nothing about
either flow itself changes. "Drive sign-in happens inside it" (§4.10)
just means Drive's OAuth is kicked off from here, not redrawn inside
this dialog - the consent screen is an external browser window
regardless, outside the app's control.
"""

from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from jarvis.modules.desktop_app import theme

FOLDER = "folder"
DRIVE = "drive"

_ICON_SIZE = 20


class _OptionTile(QPushButton):
    """A whole-tile button: icon, name and a one-line description. The
    name is also the accessible name, since the button has no text of
    its own."""

    def __init__(self, title: str, description: str, svg: str, parent: QWidget = None):
        super().__init__(parent)
        self.setObjectName("sourceOptionTile")
        self.setAccessibleName(title)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

        icon_tile = QLabel()
        icon_tile.setObjectName("sourceIconTile")
        icon_tile.setFixedSize(theme.SOURCE_ICON_TILE, theme.SOURCE_ICON_TILE)
        icon_tile.setAlignment(Qt.AlignmentFlag.AlignCenter)
        icon_tile.setPixmap(theme.icon_from_svg(svg, theme.ACCENT, _ICON_SIZE * 2).pixmap(_ICON_SIZE, _ICON_SIZE))
        title_label = QLabel(title)
        title_label.setObjectName("sourceOptionTitle")
        description_label = QLabel(description)
        description_label.setObjectName("sourceOptionDescription")
        description_label.setWordWrap(True)

        text_col = QVBoxLayout()
        text_col.setSpacing(2)
        text_col.addWidget(title_label)
        text_col.addWidget(description_label)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(theme.SPACING_LG, theme.SPACING_MD, theme.SPACING_LG, theme.SPACING_MD)
        layout.setSpacing(theme.SPACING_MD)
        layout.addWidget(icon_tile)
        layout.addLayout(text_col, 1)
        for label in (icon_tile, title_label, description_label):
            label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setMinimumHeight(72)


class AddSourceDialog(QDialog):
    def __init__(self, parent: QWidget = None):
        super().__init__(parent)
        self.setWindowTitle("Add a source")
        self.setModal(True)
        self.setMinimumWidth(440)
        self._chosen: Optional[str] = None

        heading = QLabel("Where should Jarvis look for files?")
        heading.setObjectName("dialogTitle")

        self.folder_tile = _OptionTile("Folder", "A folder on this computer.", theme.FOLDER_SVG)
        self.folder_tile.clicked.connect(lambda: self._choose(FOLDER))
        self.drive_tile = _OptionTile(
            "Google Drive", "A folder in your Drive. You sign in to Google next.", theme.CLOUD_SVG,
        )
        self.drive_tile.clicked.connect(lambda: self._choose(DRIVE))

        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.setProperty("buttonKind", "quiet")
        self.cancel_button.clicked.connect(self.reject)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        buttons.addWidget(self.cancel_button)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(theme.SPACING_XL, theme.SPACING_XL, theme.SPACING_XL, theme.SPACING_LG)
        layout.setSpacing(theme.SPACING_MD)
        layout.addWidget(heading)
        layout.addWidget(self.folder_tile)
        layout.addWidget(self.drive_tile)
        layout.addSpacing(theme.SPACING_SM)
        layout.addLayout(buttons)

    def _choose(self, choice: str) -> None:
        self._chosen = choice
        self.accept()

    def chosen(self) -> Optional[str]:
        return self._chosen
