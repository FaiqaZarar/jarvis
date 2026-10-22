#!/usr/bin/env bash
# Wraps PyInstaller's dist/jarvis_desktop/ onedir output into a .deb.
# Usage: package_deb.sh <version> (run from packaging/, after pyinstaller)
set -euo pipefail

version="${1:?usage: package_deb.sh <release-tag>}"
# Debian versions must start with a digit; release tags are desktop-build-YYYY-MM-DD[-runN].
deb_version="0.1.${GITHUB_RUN_NUMBER:-0}"
pkg=dist/deb/jarvis

rm -rf dist/deb
mkdir -p "$pkg/DEBIAN" "$pkg/opt/jarvis" "$pkg/usr/bin" \
  "$pkg/usr/share/applications" "$pkg/usr/share/icons/hicolor/scalable/apps"

cp -a dist/jarvis_desktop/. "$pkg/opt/jarvis/"
ln -s /opt/jarvis/jarvis_desktop "$pkg/usr/bin/jarvis"
cp assets/src/icon.svg "$pkg/usr/share/icons/hicolor/scalable/apps/jarvis.svg"

cat > "$pkg/usr/share/applications/jarvis.desktop" <<DESKTOP
[Desktop Entry]
Type=Application
Name=Jarvis
Comment=Personal memory and search
Exec=/opt/jarvis/jarvis_desktop
Icon=jarvis
Categories=Utility;
Terminal=false
DESKTOP

# Runtime libs Qt's xcb platform plugin needs; PyInstaller bundles Qt itself but not these.
cat > "$pkg/DEBIAN/control" <<CONTROL
Package: jarvis
Version: $deb_version
Section: utils
Priority: optional
Architecture: amd64
Maintainer: Jarvis <noreply@example.com>
Installed-Size: $(du -sk "$pkg/opt" | cut -f1)
Depends: libc6, libegl1, libgl1, libxkbcommon-x11-0, libxcb-cursor0, libxcb-icccm4, libxcb-image0, libxcb-keysyms1, libxcb-render-util0, libxcb-shape0, libdbus-1-3, libfontconfig1
Description: Jarvis desktop app
 Desktop app for Jarvis, packaged from the PyInstaller onedir build.
CONTROL

dpkg-deb --root-owner-group --build "$pkg" dist/Jarvis.deb
echo "Built dist/Jarvis.deb (embedded tag: $version)"
