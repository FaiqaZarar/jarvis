"""First-run / revisit-anytime settings dialog for the embedding
provider config (docs/desktop-app-DESIGN.md §8.8) - the packaged-app fix
for the `.env`-discovery gap found during manual packaging verification
(§6/§8.6): a packaged app's user has no repo checkout and no way to
hand-place a `.env` file inside the frozen build's internal folder, so
this dialog asks for it directly. Persists via `jarvis_server.internal.
env_config.save_settings` - the same module `MainWindow`'s first-run
check and the daemon's own startup both read through, not a second,
parallel place a value could get lost or drift out of sync.

Not gated shut: Cancel is always available, first run or not. The
daemon doesn't actually need a key to start (only real embed/search
calls do, which already fail with a clear, existing message - see
OpenRouterVoyageEmbedder), so there's no correctness reason to trap the
user in this dialog - only convenience in offering it up front.

Asks for the key only: the model and API base keep their saved values
and stay in the Settings page's Advanced section, and the log file is
reachable from there too ("Reveal log file"). DESIGN.md §4.7 keeps the
key out of the setup steps, so this reads as one plain request rather
than a provider configuration form.

`build_embedding_fields()` is the 3-field (API key/model/API base)
`QFormLayout` construction, factored out so `views/settings_view.py`
(the persistent Settings page) can build the identical form without
duplicating it - this dialog is still the one first-run uses (see that
view's own module docstring for why), just no longer the only place the
fields themselves are built.
"""

from typing import Optional

from PySide6.QtWidgets import (
    QDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from jarvis.modules.desktop_app import theme
from jarvis_server.internal import env_config

# DESIGN.md §6 ("Bad or missing API key") and the Settings page's helper.
_MESSAGE = "Jarvis needs an API key to read your files."
_HELPER = "Get a key from openrouter.ai. Jarvis saves it on this computer."
_LATER = "You can also add one later in Settings."


def build_api_key_field(current: dict) -> QLineEdit:
    api_key = QLineEdit(current.get("EMBEDDING_API_KEY", ""))
    api_key.setEchoMode(QLineEdit.EchoMode.Password)
    api_key.setPlaceholderText("sk-or-...")
    return api_key


def build_embedding_fields(current: dict) -> tuple[QFormLayout, QLineEdit, QLineEdit, QLineEdit]:
    """The API key/model/API base `QFormLayout`, pre-filled from
    `current` (an `env_config.read_saved_settings()` dict). Returns the
    layout plus its three fields, so a caller can drop the layout into
    its own container and keep references to the fields for saving."""
    api_key = build_api_key_field(current)
    model = QLineEdit(current.get("EMBEDDING_MODEL") or env_config.DEFAULT_MODEL)
    api_base = QLineEdit(current.get("EMBEDDING_API_BASE") or env_config.DEFAULT_API_BASE)

    form = QFormLayout()
    form.addRow("API key:", api_key)
    form.addRow("Embedding model:", model)
    form.addRow("API base URL:", api_base)
    return form, api_key, model, api_base


class SettingsDialog(QDialog):
    def __init__(self, parent: Optional[QWidget] = None, *, first_run: bool = False):
        super().__init__(parent)
        self.setWindowTitle("Add an API key")
        self.setModal(True)
        self.setMinimumWidth(440)

        self._current = env_config.read_saved_settings()
        self._api_key = build_api_key_field(self._current)

        heading = QLabel(_MESSAGE)
        heading.setObjectName("dialogTitle")
        heading.setWordWrap(True)
        helper = QLabel(f"{_HELPER} {_LATER}" if first_run else _HELPER)
        helper.setObjectName("dialogHelperText")
        helper.setWordWrap(True)
        key_label = QLabel("API key")

        self._cancel_button = QPushButton("Cancel")
        self._cancel_button.clicked.connect(self.reject)
        self._save_button = QPushButton("Save")
        self._save_button.setProperty("buttonKind", "primary")
        self._save_button.setDefault(True)
        self._save_button.clicked.connect(self._on_accept)
        self._api_key.textChanged.connect(self._update_save_enabled)
        self._update_save_enabled()

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        buttons.addWidget(self._cancel_button)
        buttons.addWidget(self._save_button)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(theme.SPACING_XL, theme.SPACING_XL, theme.SPACING_XL, theme.SPACING_LG)
        layout.setSpacing(theme.SPACING_SM)
        layout.addWidget(heading)
        layout.addWidget(helper)
        layout.addSpacing(theme.SPACING_SM)
        layout.addWidget(key_label)
        layout.addWidget(self._api_key)
        layout.addSpacing(theme.SPACING_LG)
        layout.addLayout(buttons)

    def _update_save_enabled(self) -> None:
        self._save_button.setEnabled(bool(self._api_key.text().strip()))

    def _on_accept(self) -> None:
        api_key = self._api_key.text().strip()
        if not api_key:
            return
        env_config.save_settings(
            api_key,
            self._current.get("EMBEDDING_MODEL") or env_config.DEFAULT_MODEL,
            self._current.get("EMBEDDING_API_BASE") or "",
        )
        self.accept()
