# -*- mode: python ; coding: utf-8 -*-
#
# Real packaging spike for docs/desktop-app-DESIGN.md §6 - see that
# section for the full writeup of what each of these four entries fixes
# and why (sqlite_vec's manually-loaded extension, tiktoken's plugin-
# discovery pattern, LadybugDB's dynamically-imported pybind backend,
# and the migrations SQL files PyInstaller can't detect as data on its
# own). Updated for the server/client isolation move (§8.9): the server
# implementation now lives in the separately-packaged jarvis_server
# distribution (jarvis/server/) rather than under jarvis/ itself - its
# migrations moved with it (now jarvis_server/migrations, not
# jarvis/migrations), and its real source directory needs its own
# --paths entry since PyInstaller's static analysis can't reliably trace
# through a PEP 660 editable install's import hooks the way a real
# interpreter can. Regenerate with (first, `echo -n "dev" > VERSION` in
# this directory - datas below requires the file to exist, even for a
# throwaway local build; see jarvis.modules.desktop_app.version for what
# a real build's value looks like and why "dev" here is harmless):
#   pyinstaller --name jarvis_desktop --onedir --windowed \
#     --paths .. --paths ../jarvis/server \
#     --add-data "<repo>/jarvis/server/jarvis_server/migrations;jarvis_server/migrations" \
#     --add-data "VERSION;." \
#     --add-binary "<sqlite_vec's vec0.dll path>;sqlite_vec" \
#     --hidden-import tiktoken_ext.openai_public \
#     --collect-submodules tiktoken_ext --collect-all ladybug \
#     ../jarvis/cmd/desktop_app.py
import sqlite_vec
import os
from PyInstaller.utils.hooks import collect_submodules
from PyInstaller.utils.hooks import collect_all
from PyInstaller.utils.hooks import copy_metadata

_sqlite_vec_dll = os.path.join(os.path.dirname(sqlite_vec.__file__), "vec0.dll")

# VERSION is written by build.yml right before this spec runs (the
# computed release tag, verbatim) - jarvis.modules.desktop_app.version
# reads it back from sys._MEIPASS at runtime, the same convention
# jarvis_desktop_mac.spec already uses (see that file's own comment on
# its identical datas entry). PyInstaller fails outright if this file
# doesn't exist at all (see the "Regenerate with" note above for the
# one-line fix for a manual local build).
datas = [
    ('../jarvis/server/jarvis_server/migrations', 'jarvis_server/migrations'),
    ('VERSION', '.'),
]
binaries = [(_sqlite_vec_dll, 'sqlite_vec')]
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

# console=False here, unlike this spec's own previous console=True - the
# same real, confirmed fix jarvis_desktop_mac.spec already made for Mac
# (see that file's own EXE() comment for the full writeup), applied for
# the matching reason on this platform: `file` on the console=True build
# reported "PE32+ executable for MS Windows 6.00 (console)" - the
# console PE subsystem, which means the OS auto-allocates and shows a
# visible console window on a plain double-click, alongside the actual
# Qt GUI window. Not what a normal Windows app launch should look like,
# and not needed for --daemon/--mcp for the same reasons already
# confirmed on Mac: --daemon's output already goes to LOG_FILE via
# subprocess.Popen(..., stdout=log_fh, stderr=log_fh) in
# jarvis.modules.daemon.client._spawn_daemon() - OS-level fd
# redirection, unconditional and independent of the built binary's own
# subsystem; --mcp is always spawned by its MCP client with explicit
# stdin/stdout/stderr pipes.
#
# Checked directly (not assumed) whether the GUI subsystem breaks a
# developer's own `jarvis_desktop.exe --daemon`/`--mcp` run from an
# *already-open* terminal, since Windows - unlike Mac - does have a
# real console/windowed PE distinction that could plausibly detach
# stdio in that case too. Two different results, by entrypoint:
#
# - **--mcp: confirmed unaffected.** Rebuilt with console=False, piped
#   a real `initialize` request into `jarvis_desktop.exe --mcp` from a
#   terminal with stdout/stderr captured separately: a well-formed
#   JSON-RPC response came back on stdout, logs went to stderr, cleanly
#   separated, and it auto-spawned a real daemon in the background
#   successfully. A minimal, isolated windowed-subsystem PyInstaller
#   build (no Jarvis code at all) confirmed the mechanism: `sys.stdout`/
#   `sys.stderr` are real, working `TextIOWrapper`s under console=False
#   as long as a real handle is provided (a terminal redirect, or the
#   explicit pipes an MCP client always uses) - a GUI subsystem only
#   skips auto-*allocating* a console it was never given, it doesn't
#   null working stdio it already has.
# - **--daemon: a real regression found, then genuinely fixed.** The
#   same manual-terminal test for `--daemon` (redirected to a file)
#   produced *zero* captured output under console=False, where the
#   previous console=True build produced the full, expected log lines.
#   Root-caused, not left as a mystery: `--daemon` (unlike `--mcp`)
#   routes through `jarvis.cmd.daemon._ensure_fixed_hash_seed()`, whose
#   `subprocess.run(argv, env=env)` relaunch (needed only when
#   `PYTHONHASHSEED` isn't already `0`) was losing the parent's stdio
#   for a windowed-subsystem child under the default "inherit" - proven
#   by pre-setting `PYTHONHASHSEED=0` (skipping the relaunch entirely)
#   and seeing full output return.
#
#   An initial attempt at a fix - reproduced in isolation in a
#   throwaway probe build, not applied to real code - hung
#   unpredictably and was set aside as "real complexity, not worth it
#   for a narrow case." Revisited and finished properly: passing the
#   real `sys.stdin`/`sys.stdout`/`sys.stderr` objects explicitly to
#   that `subprocess.run()` call (`jarvis.cmd.daemon.
#   _ensure_fixed_hash_seed`, not this file) fixes it cleanly, with no
#   hang and no downside found. Verified end to end on a real rebuild,
#   not just reasoned about: manual `--daemon` with output redirected
#   and no `PYTHONHASHSEED` preset now produces the identical full log
#   output the console=True build always did; `/health` responds; a
#   real `/shutdown` stops it cleanly with both the relaunched daemon
#   process and the original wrapper process exiting on their own, no
#   orphan. The normal auto-start path
#   (`daemon_client._spawn_daemon()`, which already sets
#   `PYTHONHASHSEED=0` before spawning and skips this relaunch
#   entirely) and `--mcp` (which never calls
#   `_ensure_fixed_hash_seed()` at all) were both re-verified working
#   identically after this change - neither is on the code path this
#   touched, and neither showed any difference.
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
