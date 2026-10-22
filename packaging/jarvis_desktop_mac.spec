# -*- mode: python ; coding: utf-8 -*-
#
# macOS counterpart to jarvis_desktop.spec (see that file's own header
# for the shared reasoning behind the tiktoken/ladybug fixes - unchanged
# here, since both ship real macOS wheels, confirmed directly against
# PyPI during the packaging investigation). Run with a Homebrew-installed
# Python (python@3.13), not python.org's official macOS build or the one
# actions/setup-python installs by default - confirmed directly on a
# real macos-latest GitHub Actions runner (docs/mac-packaging-
# investigation.yml) that the official build has no
# sqlite3.Connection.enable_load_extension() at all (AttributeError, not
# a permissions error), which sqlite-vec requires; Homebrew's own SQLite
# is built with it enabled. This is a property of *which Python runs
# PyInstaller*, not of anything in this file - see
# .github/workflows/build.yml's build-macos job for how that's ensured.
#
# Real, confirmed differences from the Windows spec:
#   - sqlite_vec ships vec0.dylib on macOS, not vec0.dll (both
#     confirmed to exist as real PyPI wheels for this exact pinned
#     version during the investigation).
#   - A BUNDLE() step is required for a real, double-clickable .app -
#     the Windows spec has no equivalent; COLLECT() alone produces a
#     plain onedir folder, not something Finder treats as an application.
#   - Uses assets/icon.icns (source: assets/src/icon.svg, regenerate
#     per assets/src/README.md) - PySide6's own fallback .icns is what
#     `icon=None` used to produce here, before this file existed.
#   - Both specs now build console=False (the Windows spec matched this
#     fix afterward, for the equivalent Windows PE subsystem reason -
#     see jarvis_desktop.spec's own EXE() comment for that platform's
#     verification, which briefly found - then fixed - one narrow
#     Windows-specific gap this Mac fix didn't have: a manual --daemon
#     run from an already-open terminal losing live output via a
#     hash-seed relaunch subprocess unrelated to anything in this file;
#     that relaunch now passes stdio through explicitly and the gap is
#     closed, not just documented).
#
# Regenerate with (first, `echo -n "dev" > VERSION` in this directory -
# datas below requires the file to exist, even for a throwaway local
# build; see jarvis.modules.desktop_app.version for what a real build's
# value looks like and why "dev" here is harmless):
#   pyinstaller --name jarvis_desktop --onedir --windowed \
#     --paths .. --paths ../jarvis/server \
#     --add-data "<repo>/jarvis/server/jarvis_server/migrations;jarvis_server/migrations" \
#     --add-binary "<sqlite_vec's vec0.dylib path>;sqlite_vec" \
#     --hidden-import tiktoken_ext.openai_public \
#     --collect-submodules tiktoken_ext --collect-all ladybug \
#     ../jarvis/cmd/desktop_app.py
import sqlite_vec
import os
from PyInstaller.utils.hooks import collect_submodules
from PyInstaller.utils.hooks import collect_all
from PyInstaller.utils.hooks import copy_metadata

_sqlite_vec_dylib = os.path.join(os.path.dirname(sqlite_vec.__file__), "vec0.dylib")

# VERSION is written by build.yml right before this spec runs (the
# computed release tag, verbatim) - jarvis.modules.desktop_app.version
# reads it back from sys._MEIPASS at runtime. PyInstaller fails outright
# if this file doesn't exist at all (see the "Regenerate with" note
# above for the one-line fix for a manual local build) - but once a
# build exists, a version.py that can't read a real tag out of it for
# any reason (blank content, wrong content) is exactly what None's safe
# "don't check for updates" fallback is for, not a build-time concern.
datas = [
    ('../jarvis/server/jarvis_server/migrations', 'jarvis_server/migrations'),
    ('VERSION', '.'),
]
binaries = [(_sqlite_vec_dylib, 'sqlite_vec')]
hiddenimports = ['tiktoken_ext.openai_public']
hiddenimports += collect_submodules('tiktoken_ext')
tmp_ret = collect_all('ladybug')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
# magika (markitdown[docx]'s transitive dep) ships its ML model as
# package data (magika/models/...), loaded relative to the installed
# package at runtime - not importable modules, so static analysis can't
# find it. Same class of problem as ladybug above (issue #114).
tmp_ret = collect_all('magika')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
# OpenTelemetry finds its context, propagators and resource detectors
# through package entry points at runtime, which PyInstaller's static
# analysis can't see - ship the metadata and the modules they point to.
datas += copy_metadata('opentelemetry-api') + copy_metadata('opentelemetry-sdk')
hiddenimports += collect_submodules('opentelemetry')


a = Analysis(
    ['../jarvis/cmd/desktop_app.py'],
    pathex=['..', '../jarvis/server'],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

# console=False here - the Windows spec now matches this too (see its
# own EXE() comment), but this was fixed here first, so the reasoning
# below predates and is independent of that.
#
# console=True was originally set to match the Windows spec, on the
# assumption --daemon/--mcp needed it for visible output. A real build
# with that setting showed the actual cost: PyInstaller writes
# LSBackgroundOnly=true into Jarvis.app's Info.plist for a console=True
# .app, which means double-clicking it in Finder never behaves like a
# normal Mac app - no Dock icon, no foreground activation - even though
# the GUI is the primary way a user launches it.
#
# Checked directly whether --daemon/--mcp actually need this before
# changing it: they don't, on either platform.
#   - --daemon: jarvis.modules.daemon.client._spawn_daemon() explicitly
#     redirects the child's stdout/stderr to LOG_FILE via
#     subprocess.Popen(..., stdout=log_fh, stderr=log_fh) *before* the
#     process starts - this is OS-level file-descriptor redirection,
#     unconditional and independent of whatever console setting the
#     binary was built with. The daemon's output was never going to a
#     visible console either way; the log file is already, and always
#     was, the only real diagnostic path (confirmed throughout the
#     JARVIS_DAEMON_HOME investigation - every real finding there came
#     from reading .jarvis_daemon.log, never from watching a console).
#   - --mcp: an MCP client (Claude Desktop, or a real stdio test client)
#     always spawns it with explicit stdin/stdout/stderr pipes - that's
#     how the MCP stdio transport works at all. jarvis/cmd/mcp_server.py's
#     own docstring already documents this: stdout carries JSON-RPC
#     frames, stderr carries logs. A piped subprocess writes to whatever
#     file descriptors it's given regardless of the target binary's
#     console/windowed setting - that setting only matters for what the
#     OS attaches by default on an unredirected double-click launch,
#     which is exactly the GUI case this fix is for, not --daemon/--mcp.
#
# A developer manually running `Jarvis.app/Contents/MacOS/jarvis_desktop
# --daemon` from Terminal by path still sees output live in that
# terminal too - macOS, unlike Windows, has no windowed/console PE-style
# split that detaches stdio when a "windowed" binary is run directly
# from a shell.
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='jarvis_desktop',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='jarvis_desktop',
)

# The one real structural difference from the Windows spec: macOS needs
# an actual .app bundle to be a real, double-clickable application.
# bundle_identifier is a placeholder ("com.jarvis.desktopapp") - revisit
# once real branding/signing ownership is decided; unsigned bundles still
# run locally (with a Gatekeeper right-click-Open the first time), which
# is enough to prove packaging works, not enough to distribute publicly.
app = BUNDLE(
    coll,
    name='Jarvis.app',
    icon='assets/icon.icns',
    bundle_identifier='com.jarvis.desktopapp',
)
