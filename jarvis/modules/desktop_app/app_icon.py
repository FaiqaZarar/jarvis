"""The window/taskbar/dev-mode-dock icon - distinct from the packaged
macOS .app's own Dock icon, which PyInstaller already bakes in at build
time from `packaging/assets/icon.icns`
(`jarvis_desktop_mac.spec`'s BUNDLE step) and needs no runtime code at
all. `QApplication.setWindowIcon()` was never called anywhere, so every
other icon slot Qt draws - the title bar, Alt-Tab/Cmd-Tab switcher, a
Windows taskbar button, the Dock in dev-mode (`python -m ...`, no .app
bundle to source an icon from) - fell back to a generic Python/Qt glyph.

Inlined as an SVG string (copied from `assets/src/icon.svg`, the same
source the packaged icon is rasterized from) rather than loaded from
that file path, matching `views/nav_rail.py`'s existing convention for
this app's other icons - so this needs no new PyInstaller `datas` entry
to ship, and dev mode and a frozen build resolve it identically.
"""

from PySide6.QtCore import QByteArray, Qt
from PySide6.QtGui import QIcon, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer

_ICON_SVG = """
<svg xmlns="http://www.w3.org/2000/svg" width="1024" height="1024" viewBox="0 0 1024 1024">
  <defs>
    <linearGradient id="bg" x1="0" y1="0" x2="0.15" y2="1">
      <stop offset="0" stop-color="#2c2f38"/>
      <stop offset="1" stop-color="#17181c"/>
    </linearGradient>
    <linearGradient id="ring" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0" stop-color="#7aa4f2"/>
      <stop offset="1" stop-color="#4670c9"/>
    </linearGradient>
    <radialGradient id="sheen" cx="0.3" cy="0.15" r="0.9">
      <stop offset="0" stop-color="#ffffff" stop-opacity="0.10"/>
      <stop offset="0.5" stop-color="#ffffff" stop-opacity="0.0"/>
    </radialGradient>
    <filter id="glow" x="-60%" y="-60%" width="220%" height="220%">
      <feGaussianBlur stdDeviation="22" result="blur"/>
      <feMerge>
        <feMergeNode in="blur"/>
        <feMergeNode in="SourceGraphic"/>
      </feMerge>
    </filter>
  </defs>

  <rect x="100" y="100" width="824" height="824" rx="184" fill="url(#bg)"/>
  <rect x="100" y="100" width="824" height="824" rx="184" fill="url(#sheen)"/>

  <g filter="url(#glow)">
    <circle cx="512" cy="512" r="228" fill="none" stroke="url(#ring)" stroke-width="34" stroke-linecap="round"
      stroke-dasharray="1114 320" transform="rotate(-56 512 512)"/>
  </g>

  <circle cx="512" cy="512" r="228" fill="none" stroke="#5b8def" stroke-width="6" opacity="0.35"/>

  <circle cx="708" cy="336" r="26" fill="#e7e9ee"/>
  <circle cx="708" cy="336" r="26" fill="#7aa4f2" opacity="0.5"/>

  <text x="512" y="600" font-size="300" font-weight="600" fill="#f1f2f5" text-anchor="middle"
    font-family="-apple-system, Helvetica Neue, Helvetica, Arial, sans-serif" letter-spacing="-4">J</text>
</svg>
"""

# Several sizes in one QIcon so Qt picks the sharpest source for each
# consumer (a 16px title bar vs. a 128px Alt-Tab preview) instead of
# scaling a single pixmap.
_SIZES = (16, 24, 32, 48, 64, 128, 256)


def build_app_icon() -> QIcon:
    renderer = QSvgRenderer(QByteArray(_ICON_SVG.encode("utf-8")))
    icon = QIcon()
    for size in _SIZES:
        pixmap = QPixmap(size, size)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        renderer.render(painter)
        painter.end()
        icon.addPixmap(pixmap)
    return icon
