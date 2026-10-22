# -*- mode: python ; coding: utf-8 -*-
#
# Linux counterpart to jarvis_desktop.spec / jarvis_desktop_mac.spec (see
# those files' headers for the shared tiktoken/ladybug/magika reasoning -
# unchanged here). Differences: sqlite_vec ships vec0.so on Linux, and
# there's no BUNDLE() step - COLLECT()'s onedir output is what
# package_deb.sh wraps into a .deb. console=False has no effect on Linux
# (no PE-subsystem equivalent) but keeps the three specs uniform.
#
# Regenerate with (first, `echo -n "dev" > VERSION` in this directory):
#   pyinstaller --name jarvis_desktop --onedir --windowed \
#     --paths .. --paths ../jarvis/server \
#     --add-data "<repo>/jarvis/server/jarvis_server/migrations:jarvis_server/migrations" \
#     --add-binary "<sqlite_vec's vec0.so path>:sqlite_vec" \
#     --hidden-import tiktoken_ext.openai_public \
#     --collect-submodules tiktoken_ext --collect-all ladybug \
#     ../jarvis/cmd/desktop_app.py
import sqlite_vec
import os
from PyInstaller.utils.hooks import collect_submodules
from PyInstaller.utils.hooks import collect_all
from PyInstaller.utils.hooks import copy_metadata

_sqlite_vec_so = os.path.join(os.path.dirname(sqlite_vec.__file__), "vec0.so")

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
binaries = [(_sqlite_vec_so, 'sqlite_vec')]
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
