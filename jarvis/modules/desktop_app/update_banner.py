"""Non-blocking "an update is available" banner.

Not a subclass of `error_presentation.DaemonStatusBanner` - that widget's
contract is exactly one message plus one Dismiss action; this one needs
two distinct, per-state-relabelled actions (`update_clicked`/`later_clicked`
mean "Update", "Restart now" or "Retry" depending which `show_*` method
last ran - `MainWindow` tracks which, this widget only renders) plus an
in-place download-progress state, different enough to not force into the
existing shape. Reuses the same fade-in/out `QPropertyAnimation` plumbing
for visual consistency with the daemon-status banner.
"""

from typing import Optional

from PySide6.QtCore import Qt, QPropertyAnimation, Signal
from PySide6.QtWidgets import QGraphicsOpacityEffect, QLabel, QProgressBar, QPushButton, QWidget

from jarvis.modules.desktop_app.error_presentation import banner_layout, banner_markup

_FADE_DURATION_MS = 180


class UpdateBanner(QWidget):
    update_clicked = Signal()
    later_clicked = Signal()

    def __init__(self, parent: QWidget = None):
        super().__init__(parent)
        # "Update ready" is DESIGN.md's info kind (blue), not amber - an
        # update isn't something that needs attention the way a warning
        # does. Needs both the object name (for the QSS selector below)
        # and WA_StyledBackground (a plain QWidget subclass otherwise
        # never paints its own QSS background/border/radius at all).
        self.setObjectName("updateBanner")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._message = ""
        self._label = QLabel()
        self._label.setWordWrap(True)
        self._label.setTextFormat(Qt.TextFormat.RichText)
        self._label.setObjectName("bannerLabel")

        self._progress = QProgressBar()
        self._progress.setVisible(False)

        self._update_button = QPushButton("Update && Restart")
        self._update_button.clicked.connect(self.update_clicked)
        self._later_button = QPushButton("Later")
        self._later_button.clicked.connect(self._on_later)

        layout = banner_layout(self)
        layout.addWidget(self._label, 1)
        layout.addWidget(self._progress, 1)
        layout.addWidget(self._update_button)
        layout.addWidget(self._later_button)

        self._opacity = QGraphicsOpacityEffect(self)
        self._opacity.setOpacity(0.0)
        self.setGraphicsEffect(self._opacity)

        self._fade_in_anim = QPropertyAnimation(self._opacity, b"opacity", self)
        self._fade_in_anim.setDuration(_FADE_DURATION_MS)
        self._fade_in_anim.setEndValue(1.0)

        self._fade_out_anim = QPropertyAnimation(self._opacity, b"opacity", self)
        self._fade_out_anim.setDuration(_FADE_DURATION_MS)
        self._fade_out_anim.setEndValue(0.0)
        self._fade_out_anim.finished.connect(self.hide)

        self.hide()

    def show_update_available(self, tag: str) -> None:
        self._set_message(f"Jarvis {tag} is available.")
        self._progress.setVisible(False)
        self._update_button.setText("Update")
        self._update_button.setVisible(True)
        self._update_button.setEnabled(True)
        self._later_button.setText("Later")
        self._later_button.setVisible(True)
        self._fade_in()

    def show_downloading(self) -> None:
        self._set_message("Downloading the update. You can keep working.")
        self._update_button.setVisible(False)
        self._later_button.setVisible(False)
        self._progress.setVisible(True)
        self._progress.setRange(0, 0)  # indeterminate until the first real progress value
        self._fade_in()

    def set_progress(self, downloaded: int, total: Optional[int]) -> None:
        if total:
            self._progress.setRange(0, total)
            self._progress.setValue(downloaded)
            self._set_message(
                f"Downloading the update. {downloaded / 1e6:.1f} of {total / 1e6:.1f} MB. "
                "You can keep working."
            )
        else:
            self._progress.setRange(0, 0)

    def show_ready(self) -> None:
        self._set_message(
            "Update ready. Restart Jarvis to finish. Your current version keeps working until then."
        )
        self._progress.setVisible(False)
        self._update_button.setText("Restart now")
        self._update_button.setVisible(True)
        self._update_button.setEnabled(True)
        self._later_button.setText("Later")
        self._later_button.setVisible(True)
        self._fade_in()

    def show_installing(self) -> None:
        self._set_message("Installing the update. Jarvis will reopen in a moment.")
        self._progress.setVisible(False)
        self._update_button.setVisible(False)
        self._later_button.setVisible(False)
        self._fade_in()

    def show_download_failed(self) -> None:
        self._set_message("The update didn't download. Your current version keeps working.")
        self._progress.setVisible(False)
        self._update_button.setText("Retry")
        self._update_button.setVisible(True)
        self._update_button.setEnabled(True)
        self._later_button.setVisible(False)
        self._fade_in()

    def show_install_failed(self) -> None:
        self._set_message("The update couldn't be installed. Your current version keeps working.")
        self._progress.setVisible(False)
        self._update_button.setText("Retry")
        self._update_button.setVisible(True)
        self._update_button.setEnabled(True)
        self._later_button.setText("Download manually")
        self._later_button.setVisible(True)
        self._fade_in()

    def show_blocked(self, message: str) -> None:
        """A failure no retry can fix from here (e.g. macOS App
        Translocation) - just the message and a way to dismiss it."""
        self._set_message(message)
        self._progress.setVisible(False)
        self._update_button.setVisible(False)
        self._later_button.setText("Dismiss")
        self._later_button.setVisible(True)
        self._fade_in()

    def message(self) -> str:
        return self._message

    def _set_message(self, message: str) -> None:
        self._message = message
        self._label.setText(banner_markup(message))

    def dismiss(self) -> None:
        if self.isHidden():
            return
        self._fade_in_anim.stop()
        self._fade_out_anim.stop()
        self._fade_out_anim.setStartValue(self._opacity.opacity())
        self._fade_out_anim.start()

    def _fade_in(self) -> None:
        self._fade_out_anim.stop()
        self.show()
        self._fade_in_anim.stop()
        self._fade_in_anim.setStartValue(self._opacity.opacity())
        self._fade_in_anim.start()

    def _on_later(self) -> None:
        self.later_clicked.emit()
        self.dismiss()
