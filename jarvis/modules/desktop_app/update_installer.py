"""Installs a downloaded update over the running app and relaunches it -
a Mac `.dmg`/`.app` swap or a Windows silent installer re-run, dispatched
by `sys.platform` in `trigger_install_and_relaunch()` at the bottom of
this module.

The hard part of a self-updater, on either platform: the running
process's own executable lives inside (or next to) whatever the update
needs to overwrite, so nothing running *as that process* can safely do
the swap. Both platform paths below write a small, detached helper
script - not part of the frozen Python binary, so it survives the
binary being replaced - and launch it just before the GUI quits. The
script waits for this process (and the daemon) to actually exit, then
does the platform-specific install/relaunch dance on its own.

Everything either script does is logged to
`<daemon_home>/.jarvis_updater.log` (the same convention as the daemon's
own `.jarvis_daemon.log`), left on disk for post-mortem debugging on any
failure - only a fully successful run deletes the downloaded update file
and the script itself.
"""

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Optional

from jarvis.modules.daemon import client as daemon_client
from jarvis.modules.daemon.client import DaemonError

# Bounded wait for the GUI/daemon processes to actually exit before the
# script gives up waiting and proceeds anyway - better to risk a mid-swap
# collision on an unresponsive process than to hang forever with the user
# staring at nothing.
_HELPER_WAIT_TIMEOUT_S = 20


class UpdateBlockedError(RuntimeError):
    """Raised instead of a plain `RuntimeError` when the update genuinely
    cannot proceed from here and no retry will help - distinct from the
    dev-build "not running from a packaged .app/.exe" case, which a
    plain `RuntimeError` still covers."""


def _is_translocated() -> bool:
    """True under macOS Gatekeeper App Translocation - same check as
    `claude_desktop_setup._is_translocated()` (duplicated, not shared -
    this codebase's established per-module-seam convention). A
    translocated `.app` runs from a randomized, read-only copy, so the
    file swap below would silently write to a path that vanishes on the
    next relaunch rather than actually updating anything."""
    return sys.platform == "darwin" and "AppTranslocation" in sys.executable


def _running_app_bundle_path() -> Optional[Path]:
    """`.../Jarvis.app/Contents/MacOS/jarvis_desktop` -> `.../Jarvis.app`
    (per `jarvis_desktop_mac.spec`'s `EXE(name='jarvis_desktop')` inside
    `BUNDLE(name='Jarvis.app')`) - derived from `sys.executable`, never
    hardcoded to `/Applications`, since a real install could sit anywhere
    the user dragged it to. `None` outside a frozen build - `sys.executable`
    there is a real Python interpreter, not something inside an `.app`."""
    if not getattr(sys, "frozen", False):
        return None
    exe = Path(sys.executable).resolve()
    candidate = exe.parent.parent.parent
    return candidate if candidate.suffix == ".app" else None


def _read_daemon_pid() -> Optional[int]:
    try:
        info = json.loads(daemon_client.DISCOVERY_FILE.read_text())
    except (OSError, ValueError):
        return None
    pid = info.get("pid")
    return pid if isinstance(pid, int) else None


def _running_exe_path() -> Optional[Path]:
    """`sys.executable` when frozen on Windows - the running
    `jarvis_desktop.exe` inside its Inno Setup install directory
    (`jarvis_installer.iss`'s `{app}`). Unlike the Mac `.app` bundle
    above, there's no separate bundle root to derive - the exe itself is
    exactly what the installer overwrites in place, so this is just the
    resolved path, not a parent-directory walk. `None` outside a frozen
    build, same convention as `_running_app_bundle_path`."""
    if not getattr(sys, "frozen", False):
        return None
    return Path(sys.executable).resolve()


def _build_mac_helper_script(
    *, gui_pid: int, daemon_pid: Optional[int], app_path: Path, dmg_path: Path, log_path: Path,
) -> str:
    """Copy-then-swap, never delete-then-copy: the new `.app` is copied to
    a staging path and only swapped into place once that copy has fully
    succeeded (`mv`, not overwrite-in-place) - the old `.app` is renamed
    aside, not deleted, until the new one is confirmed in place. Combined
    with `set -euo pipefail`, any failure before the final cleanup line
    leaves either the untouched original `.app` or the original safely
    parked at `<name>.app.old` - never a state with no app at all."""
    q = shlex.quote
    app = q(str(app_path))
    dmg = q(str(dmg_path))
    daemon_wait = f"wait_for_pid {daemon_pid}" if daemon_pid is not None else ": # no daemon pid known"

    return f"""#!/bin/bash
set -euo pipefail
exec >>{q(str(log_path))} 2>&1
echo "=== Jarvis updater: $(date -u) ==="

wait_for_pid() {{
    local pid="$1" waited=0
    while kill -0 "$pid" 2>/dev/null; do
        if [ "$waited" -ge {_HELPER_WAIT_TIMEOUT_S} ]; then
            echo "Timed out waiting for pid $pid to exit - proceeding anyway"
            break
        fi
        sleep 1
        waited=$((waited + 1))
    done
}}

wait_for_pid {gui_pid}
{daemon_wait}

MOUNT_DIR="$(mktemp -d)"
hdiutil attach -nobrowse -mountpoint "$MOUNT_DIR" {dmg}
xattr -cr "$MOUNT_DIR/Jarvis.app"

STAGING={app}.new
rm -rf "$STAGING"
cp -R "$MOUNT_DIR/Jarvis.app" "$STAGING"
xattr -cr "$STAGING"

mv {app} {app}.old
mv "$STAGING" {app}
rm -rf {app}.old

hdiutil detach "$MOUNT_DIR"

open -n {app}

rm -f {dmg}
rm -f "$0"
"""


def _trigger_mac_install_and_relaunch(dmg_path: Path, client_id: Optional[str]) -> None:
    """Best-effort daemon shutdown, then write and launch the detached
    bash helper script - the caller (`MainWindow`) is expected to quit
    the GUI's own `QApplication` immediately after this returns."""
    if _is_translocated():
        raise UpdateBlockedError("Move Jarvis to your Applications folder and reopen it to update.")

    if client_id is not None:
        try:
            daemon_client.request_shutdown(client_id)
        except DaemonError:
            pass

    app_path = _running_app_bundle_path()
    if app_path is None:
        raise RuntimeError("Cannot install update: not running from a packaged .app.")

    daemon_home = daemon_client.DISCOVERY_FILE.parent
    updates_dir = daemon_home / "updates"
    updates_dir.mkdir(parents=True, exist_ok=True)
    script_path = updates_dir / "install_update.sh"

    script = _build_mac_helper_script(
        gui_pid=os.getpid(),
        daemon_pid=_read_daemon_pid(),
        app_path=app_path,
        dmg_path=dmg_path,
        log_path=daemon_home / ".jarvis_updater.log",
    )
    script_path.write_text(script)
    script_path.chmod(0o700)

    subprocess.Popen(
        ["/bin/bash", str(script_path)],
        start_new_session=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _build_windows_helper_script(
    *, gui_pid: int, daemon_pid: Optional[int], exe_path: Path, installer_path: Path, log_path: Path,
) -> str:
    """PowerShell instead of the Mac path's bash, for the same reason:
    a detached script outside the frozen binary, so it survives that
    binary being replaced out from under it. No copy-then-swap dance
    needed here like the Mac path's - Inno Setup's own silent re-install
    (`jarvis_installer.iss`) already does a safe file-by-file overwrite
    of `{app}` (its own `ignoreversion` install already handles that);
    this script's only job is waiting for the old process to actually
    let go of its files first, since Windows locks a running exe's own
    files, then letting the installer run silently and relaunching."""
    daemon_wait = (
        f"Wait-ForExit -TargetPid {daemon_pid}" if daemon_pid is not None
        else "# no daemon pid known"
    )

    return f"""
$LogPath = {_ps_quote(str(log_path))}
Start-Transcript -Path $LogPath -Append | Out-Null
Write-Output "=== Jarvis updater: $(Get-Date) ==="

function Wait-ForExit {{
    param([int]$TargetPid)
    $waited = 0
    while ($true) {{
        $proc = Get-Process -Id $TargetPid -ErrorAction SilentlyContinue
        if ($null -eq $proc) {{ break }}
        if ($waited -ge {_HELPER_WAIT_TIMEOUT_S}) {{
            Write-Output "Timed out waiting for pid $TargetPid to exit - proceeding anyway"
            break
        }}
        Start-Sleep -Seconds 1
        $waited += 1
    }}
}}

Wait-ForExit -TargetPid {gui_pid}
{daemon_wait}

$InstallerPath = {_ps_quote(str(installer_path))}
$ExePath = {_ps_quote(str(exe_path))}

# Same silent-install flags jarvis_installer.iss's own Inno Setup build
# supports for a normal end-user install - /SP- skips the "This will
# install..." prompt, /VERYSILENT and /SUPPRESSMSGBOX skip every other
# window and message box, /NORESTART never reboots on this app's behalf.
$proc = Start-Process -FilePath $InstallerPath -ArgumentList "/VERYSILENT","/SUPPRESSMSGBOX","/NORESTART","/SP-" -Wait -PassThru
if ($proc.ExitCode -ne 0) {{
    Write-Output "Installer exited with code $($proc.ExitCode) - not relaunching or cleaning up"
    Stop-Transcript | Out-Null
    exit 1
}}

Start-Process -FilePath $ExePath

Remove-Item -Force $InstallerPath -ErrorAction SilentlyContinue
Stop-Transcript | Out-Null
Remove-Item -Force $MyInvocation.MyCommand.Path -ErrorAction SilentlyContinue
"""


def _ps_quote(value: str) -> str:
    """PowerShell single-quoted string literal - only `'` itself needs
    escaping (doubled), unlike shlex.quote's POSIX-shell rules used for
    the Mac helper script above."""
    return "'" + value.replace("'", "''") + "'"


def _trigger_windows_install_and_relaunch(installer_path: Path, client_id: Optional[str]) -> None:
    """Best-effort daemon shutdown, then write and launch the detached
    PowerShell helper script - the caller (`MainWindow`) is expected to
    quit the GUI's own `QApplication` immediately after this returns,
    same contract as `_trigger_mac_install_and_relaunch`."""
    if client_id is not None:
        try:
            daemon_client.request_shutdown(client_id)
        except DaemonError:
            pass

    exe_path = _running_exe_path()
    if exe_path is None:
        raise RuntimeError("Cannot install update: not running from a packaged .exe.")

    daemon_home = daemon_client.DISCOVERY_FILE.parent
    updates_dir = daemon_home / "updates"
    updates_dir.mkdir(parents=True, exist_ok=True)
    script_path = updates_dir / "install_update.ps1"

    script = _build_windows_helper_script(
        gui_pid=os.getpid(),
        daemon_pid=_read_daemon_pid(),
        exe_path=exe_path,
        installer_path=installer_path,
        log_path=daemon_home / ".jarvis_updater.log",
    )
    script_path.write_text(script)

    subprocess.Popen(
        ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script_path)],
        creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def trigger_install_and_relaunch(update_path: Path, client_id: Optional[str]) -> None:
    """Dispatches to the platform-appropriate install-and-relaunch path,
    by `sys.platform` - `MainWindow` calls this one entry point without
    needing to know which platform it's running on; `update_path` is
    whatever `update_downloader.py` saved (a `.dmg` on Mac, a
    `JarvisSetup.exe`-shaped installer on Windows)."""
    if sys.platform == "darwin":
        _trigger_mac_install_and_relaunch(update_path, client_id)
    elif sys.platform == "win32":
        _trigger_windows_install_and_relaunch(update_path, client_id)
    else:
        raise RuntimeError(f"Updating isn't supported on this platform ({sys.platform}).")
