"""jarvis.modules.desktop_app.update_installer - pure path math for
locating the running .app/.exe, the generated helper scripts' shape/
quoting, and each platform trigger function's call ordering. Real
hdiutil mount/replace/relaunch (Mac) or Inno Setup silent-install
(Windows) behavior needs a real machine of that platform by hand; nothing
here spawns a real subprocess.

Tests below call the platform-specific `_trigger_mac_install_and_relaunch`/
`_trigger_windows_install_and_relaunch` directly (not the public
`trigger_install_and_relaunch`) precisely so they stay meaningful
regardless of which platform actually runs this test suite - the public
function's own dispatch is exercised separately, by monkeypatching
`sys.platform` itself (see the "trigger_install_and_relaunch dispatch"
section at the bottom)."""

import json
import shlex
import sys

import pytest

from jarvis.modules.daemon.client import DaemonConnectionError
from jarvis.modules.desktop_app import update_installer


@pytest.fixture(autouse=True)
def _no_real_frozen_state(monkeypatch):
    monkeypatch.delattr(sys, "frozen", raising=False)


# ---------------------------------------------------------------------
# _running_app_bundle_path
# ---------------------------------------------------------------------

def test_running_app_bundle_path_is_none_when_not_frozen():
    assert update_installer._running_app_bundle_path() is None


@pytest.mark.skipif(
    sys.platform != "darwin",
    reason="Path(...).resolve() on a Mac-shaped absolute path only behaves like a POSIX "
           "path on a real POSIX host - on Windows it's resolved as a WindowsPath instead "
           "(e.g. gaining a drive letter), which this string comparison can't account for. "
           "The function under test is itself only ever exercised on a real macOS bundle.",
)
def test_running_app_bundle_path_derived_from_a_real_bundle_layout(monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(
        sys, "executable", "/Applications/Jarvis.app/Contents/MacOS/jarvis_desktop",
    )

    assert str(update_installer._running_app_bundle_path()) == "/Applications/Jarvis.app"


def test_running_app_bundle_path_is_none_for_a_dev_interpreter_path(monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", "/usr/local/bin/python3.13")

    assert update_installer._running_app_bundle_path() is None


# ---------------------------------------------------------------------
# _read_daemon_pid
# ---------------------------------------------------------------------

def test_read_daemon_pid_from_a_real_discovery_file(monkeypatch, tmp_path):
    discovery = tmp_path / ".jarvis_daemon.json"
    discovery.write_text(json.dumps({"port": 1234, "pid": 5678}))
    monkeypatch.setattr(update_installer.daemon_client, "DISCOVERY_FILE", discovery)

    assert update_installer._read_daemon_pid() == 5678


def test_read_daemon_pid_returns_none_when_discovery_file_is_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(update_installer.daemon_client, "DISCOVERY_FILE", tmp_path / "nope.json")

    assert update_installer._read_daemon_pid() is None


# ---------------------------------------------------------------------
# _build_mac_helper_script - string-containment + quoting checks
# ---------------------------------------------------------------------

def _script(tmp_path, **overrides):
    kwargs = dict(
        gui_pid=111,
        daemon_pid=222,
        app_path=tmp_path / "Jarvis.app",
        dmg_path=tmp_path / "Jarvis-desktop-build-2026-02-01.dmg",
        log_path=tmp_path / ".jarvis_updater.log",
    )
    kwargs.update(overrides)
    return update_installer._build_mac_helper_script(**kwargs)


def test_helper_script_waits_for_both_pids(tmp_path):
    script = _script(tmp_path)
    assert "wait_for_pid 111" in script
    assert "wait_for_pid 222" in script


def test_helper_script_skips_daemon_wait_when_pid_unknown(tmp_path):
    script = _script(tmp_path, daemon_pid=None)
    assert "wait_for_pid 111" in script
    assert "no daemon pid known" in script


def test_helper_script_mounts_xattrs_and_relaunches(tmp_path):
    script = _script(tmp_path)
    assert "hdiutil attach -nobrowse" in script
    assert "xattr -cr" in script
    assert "hdiutil detach" in script
    assert "open -n" in script


def test_helper_script_copies_then_swaps_never_deletes_before_copying(tmp_path):
    app_path = tmp_path / "Jarvis.app"
    script = _script(tmp_path, app_path=app_path)
    lines = script.splitlines()

    def _index_containing(needle):
        return next(i for i, line in enumerate(lines) if needle in line)

    copy_idx = _index_containing("cp -R")
    old_rename_idx = _index_containing(f"mv {shlex.quote(str(app_path))} {shlex.quote(str(app_path))}.old")
    assert copy_idx < old_rename_idx, "must copy the new app before touching the installed one at all"
    # The staging directory (<app>.new) may be pre-cleaned before the copy
    # (leftover from a prior failed update) - that's not the real
    # installed app, so it's not the invariant this test is about. The
    # real installed app itself must not be removed before the copy of
    # the new one has fully succeeded.
    before_copy = "\n".join(lines[:copy_idx])
    assert f"rm -rf {shlex.quote(str(app_path))}" not in before_copy
    assert f"rm -rf {shlex.quote(str(app_path))}.old" not in before_copy


def test_helper_script_runs_under_strict_mode_and_logs(tmp_path):
    script = _script(tmp_path)
    assert script.startswith("#!/bin/bash\nset -euo pipefail\n")
    assert "exec >>" in script


def test_helper_script_cleans_up_dmg_and_itself_only_at_the_end(tmp_path):
    script = _script(tmp_path)
    lines = script.splitlines()
    relaunch_idx = next(i for i, line in enumerate(lines) if line.startswith("open -n"))
    cleanup_lines = "\n".join(lines[relaunch_idx:])
    assert 'rm -f' in cleanup_lines
    assert '$0' in cleanup_lines


def test_helper_script_quotes_every_substituted_path(tmp_path):
    # Paths with spaces are the common case, but shlex.quote's plain
    # single-quote wrapping means a space-only path is trivially still a
    # *substring* of its own quoted form - not a meaningful check on its
    # own. A single quote character inside the path is the real risk (an
    # unescaped one would terminate the quoted string early and let the
    # rest execute as shell syntax) - real quoting must escape it.
    app_path = tmp_path / "Weird's dir" / "Jarvis.app"
    dmg_path = tmp_path / "another dir" / "Jarvis update.dmg"
    log_path = tmp_path / "log dir" / ".jarvis_updater.log"

    script = _script(tmp_path, app_path=app_path, dmg_path=dmg_path, log_path=log_path)

    assert shlex.quote(str(app_path)) in script
    assert shlex.quote(str(dmg_path)) in script
    assert shlex.quote(str(log_path)) in script
    # A naive, unescaped wrap (just '<path>') would be broken shell syntax
    # here - the embedded ' would terminate the string early. Confirm
    # shlex.quote's real escaping was actually used, not a naive wrap.
    naive_wrap = f"'{app_path}'"
    assert naive_wrap not in script


# ---------------------------------------------------------------------
# _is_translocated
# ---------------------------------------------------------------------

def test_is_translocated_false_off_darwin(monkeypatch):
    monkeypatch.setattr(update_installer.sys, "platform", "win32")
    monkeypatch.setattr(sys, "executable", "/private/var/folders/.../AppTranslocation/ABCD/d/Jarvis.app/x")

    assert update_installer._is_translocated() is False


def test_is_translocated_true_on_darwin_with_a_translocated_path(monkeypatch):
    monkeypatch.setattr(update_installer.sys, "platform", "darwin")
    monkeypatch.setattr(
        sys, "executable",
        "/private/var/folders/.../AppTranslocation/ABCD/d/Jarvis.app/Contents/MacOS/jarvis_desktop",
    )

    assert update_installer._is_translocated() is True


def test_is_translocated_false_on_darwin_with_a_normal_path(monkeypatch):
    monkeypatch.setattr(update_installer.sys, "platform", "darwin")
    monkeypatch.setattr(sys, "executable", "/Applications/Jarvis.app/Contents/MacOS/jarvis_desktop")

    assert update_installer._is_translocated() is False


# ---------------------------------------------------------------------
# _trigger_mac_install_and_relaunch
# ---------------------------------------------------------------------

def test_trigger_raises_when_not_running_from_a_packaged_app(tmp_path):
    with pytest.raises(RuntimeError, match="packaged .app"):
        update_installer._trigger_mac_install_and_relaunch(tmp_path / "x.dmg", None)


def test_trigger_raises_update_blocked_when_translocated_before_touching_anything(monkeypatch, tmp_path):
    """A translocated .app's own bundle path still looks valid (its
    suffix is still .app) - without this check the swap would silently
    write to a copy that vanishes on the next relaunch instead of ever
    raising. Must bail out before the daemon shutdown or any file I/O."""
    monkeypatch.setattr(update_installer.sys, "platform", "darwin")
    monkeypatch.setattr(
        sys, "executable",
        "/private/var/folders/.../AppTranslocation/ABCD/d/Jarvis.app/Contents/MacOS/jarvis_desktop",
    )

    def _must_not_be_called(*a, **k):
        raise AssertionError("must bail out before any shutdown/file I/O when translocated")

    monkeypatch.setattr(update_installer.daemon_client, "request_shutdown", _must_not_be_called)

    with pytest.raises(update_installer.UpdateBlockedError, match="Applications folder"):
        update_installer._trigger_mac_install_and_relaunch(tmp_path / "x.dmg", "client-123")


def test_trigger_requests_shutdown_before_launching_the_helper(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "Jarvis.app" / "Contents" / "MacOS" / "jarvis_desktop"))
    monkeypatch.setattr(update_installer.daemon_client, "DISCOVERY_FILE", tmp_path / ".jarvis_daemon.json")

    calls = []
    monkeypatch.setattr(
        update_installer.daemon_client, "request_shutdown", lambda cid: calls.append(("shutdown", cid)),
    )
    monkeypatch.setattr(
        update_installer.subprocess, "Popen", lambda *a, **k: calls.append(("popen", a, k)),
    )

    update_installer._trigger_mac_install_and_relaunch(tmp_path / "Jarvis-x.dmg", "client-123")

    assert calls[0] == ("shutdown", "client-123")
    assert calls[1][0] == "popen"


def test_trigger_swallows_a_daemon_error_from_shutdown(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "Jarvis.app" / "Contents" / "MacOS" / "jarvis_desktop"))
    monkeypatch.setattr(update_installer.daemon_client, "DISCOVERY_FILE", tmp_path / ".jarvis_daemon.json")

    def _boom(_cid):
        raise DaemonConnectionError("nope")

    monkeypatch.setattr(update_installer.daemon_client, "request_shutdown", _boom)
    monkeypatch.setattr(update_installer.subprocess, "Popen", lambda *a, **k: None)

    update_installer._trigger_mac_install_and_relaunch(tmp_path / "Jarvis-x.dmg", "client-123")  # must not raise


def test_trigger_skips_shutdown_when_no_client_id(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "Jarvis.app" / "Contents" / "MacOS" / "jarvis_desktop"))
    monkeypatch.setattr(update_installer.daemon_client, "DISCOVERY_FILE", tmp_path / ".jarvis_daemon.json")

    def _must_not_be_called(_cid):
        raise AssertionError("request_shutdown should not be called without a client_id")

    monkeypatch.setattr(update_installer.daemon_client, "request_shutdown", _must_not_be_called)
    monkeypatch.setattr(update_installer.subprocess, "Popen", lambda *a, **k: None)

    update_installer._trigger_mac_install_and_relaunch(tmp_path / "Jarvis-x.dmg", None)


@pytest.mark.skipif(
    sys.platform != "darwin",
    reason="os.chmod on Windows only toggles the read-only bit - it cannot produce real "
           "POSIX permission bits (0o700 never actually appears in st_mode there), and the "
           "generated script is a bash script that only ever runs on macOS in production.",
)
def test_trigger_writes_an_executable_script_at_the_expected_path(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "Jarvis.app" / "Contents" / "MacOS" / "jarvis_desktop"))
    monkeypatch.setattr(update_installer.daemon_client, "DISCOVERY_FILE", tmp_path / ".jarvis_daemon.json")
    monkeypatch.setattr(update_installer.daemon_client, "request_shutdown", lambda cid: None)
    monkeypatch.setattr(update_installer.subprocess, "Popen", lambda *a, **k: None)

    update_installer._trigger_mac_install_and_relaunch(tmp_path / "Jarvis-x.dmg", None)

    script_path = tmp_path / "updates" / "install_update.sh"
    assert script_path.exists()
    assert oct(script_path.stat().st_mode)[-3:] == "700"


# ---------------------------------------------------------------------
# _running_exe_path (Windows)
# ---------------------------------------------------------------------

def test_running_exe_path_is_none_when_not_frozen():
    assert update_installer._running_exe_path() is None


def test_running_exe_path_is_the_resolved_executable_when_frozen(monkeypatch, tmp_path):
    exe = tmp_path / "Jarvis" / "jarvis_desktop.exe"
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(exe))

    assert update_installer._running_exe_path() == exe.resolve()


# ---------------------------------------------------------------------
# _build_windows_helper_script - string-containment + quoting checks
# ---------------------------------------------------------------------

def _windows_script(tmp_path, **overrides):
    kwargs = dict(
        gui_pid=111,
        daemon_pid=222,
        exe_path=tmp_path / "Jarvis" / "jarvis_desktop.exe",
        installer_path=tmp_path / "JarvisSetup.exe",
        log_path=tmp_path / ".jarvis_updater.log",
    )
    kwargs.update(overrides)
    return update_installer._build_windows_helper_script(**kwargs)


def test_windows_helper_script_waits_for_both_pids(tmp_path):
    script = _windows_script(tmp_path)
    assert "Wait-ForExit -TargetPid 111" in script
    assert "Wait-ForExit -TargetPid 222" in script


def test_windows_helper_script_skips_daemon_wait_when_pid_unknown(tmp_path):
    script = _windows_script(tmp_path, daemon_pid=None)
    assert "Wait-ForExit -TargetPid 111" in script
    assert "no daemon pid known" in script


def test_windows_helper_script_installs_silently_and_relaunches(tmp_path):
    script = _windows_script(tmp_path)
    assert "/VERYSILENT" in script
    assert "/SUPPRESSMSGBOX" in script
    assert "/NORESTART" in script
    assert "/SP-" in script
    assert "Start-Process -FilePath $ExePath" in script


def test_windows_helper_script_quotes_every_substituted_path(tmp_path):
    exe_path = tmp_path / "Weird's dir" / "jarvis_desktop.exe"
    installer_path = tmp_path / "another dir" / "Jarvis's Setup.exe"
    log_path = tmp_path / "log dir" / ".jarvis_updater.log"

    script = _windows_script(tmp_path, exe_path=exe_path, installer_path=installer_path, log_path=log_path)

    assert update_installer._ps_quote(str(exe_path)) in script
    assert update_installer._ps_quote(str(installer_path)) in script
    assert update_installer._ps_quote(str(log_path)) in script
    # A naive, unescaped wrap (just '<path>') would be broken PowerShell
    # syntax here - the embedded ' would terminate the string early.
    naive_wrap = f"'{installer_path}'"
    assert naive_wrap not in script


def test_windows_helper_script_cleans_up_installer_and_itself_only_at_the_end(tmp_path):
    script = _windows_script(tmp_path)
    lines = script.splitlines()
    relaunch_idx = next(i for i, line in enumerate(lines) if line.startswith("Start-Process -FilePath $ExePath"))
    cleanup_lines = "\n".join(lines[relaunch_idx:])
    assert "Remove-Item -Force $InstallerPath" in cleanup_lines
    assert "$MyInvocation.MyCommand.Path" in cleanup_lines


# ---------------------------------------------------------------------
# _trigger_windows_install_and_relaunch
# ---------------------------------------------------------------------

def test_windows_trigger_raises_when_not_running_from_a_packaged_exe(tmp_path):
    with pytest.raises(RuntimeError, match="packaged .exe"):
        update_installer._trigger_windows_install_and_relaunch(tmp_path / "JarvisSetup.exe", None)


def test_windows_trigger_requests_shutdown_before_launching_the_helper(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "Jarvis" / "jarvis_desktop.exe"))
    monkeypatch.setattr(update_installer.daemon_client, "DISCOVERY_FILE", tmp_path / ".jarvis_daemon.json")

    calls = []
    monkeypatch.setattr(
        update_installer.daemon_client, "request_shutdown", lambda cid: calls.append(("shutdown", cid)),
    )
    monkeypatch.setattr(
        update_installer.subprocess, "Popen", lambda *a, **k: calls.append(("popen", a, k)),
    )

    update_installer._trigger_windows_install_and_relaunch(tmp_path / "JarvisSetup.exe", "client-123")

    assert calls[0] == ("shutdown", "client-123")
    assert calls[1][0] == "popen"
    assert calls[1][1][0][0] == "powershell.exe"


def test_windows_trigger_skips_shutdown_when_no_client_id(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "Jarvis" / "jarvis_desktop.exe"))
    monkeypatch.setattr(update_installer.daemon_client, "DISCOVERY_FILE", tmp_path / ".jarvis_daemon.json")

    def _must_not_be_called(_cid):
        raise AssertionError("request_shutdown should not be called without a client_id")

    monkeypatch.setattr(update_installer.daemon_client, "request_shutdown", _must_not_be_called)
    monkeypatch.setattr(update_installer.subprocess, "Popen", lambda *a, **k: None)

    update_installer._trigger_windows_install_and_relaunch(tmp_path / "JarvisSetup.exe", None)


def test_windows_trigger_writes_a_script_at_the_expected_path(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "Jarvis" / "jarvis_desktop.exe"))
    monkeypatch.setattr(update_installer.daemon_client, "DISCOVERY_FILE", tmp_path / ".jarvis_daemon.json")
    monkeypatch.setattr(update_installer.daemon_client, "request_shutdown", lambda cid: None)
    monkeypatch.setattr(update_installer.subprocess, "Popen", lambda *a, **k: None)

    update_installer._trigger_windows_install_and_relaunch(tmp_path / "JarvisSetup.exe", None)

    script_path = tmp_path / "updates" / "install_update.ps1"
    assert script_path.exists()


# ---------------------------------------------------------------------
# trigger_install_and_relaunch dispatch
# ---------------------------------------------------------------------

def test_dispatch_calls_mac_trigger_on_darwin(monkeypatch, tmp_path):
    monkeypatch.setattr(update_installer.sys, "platform", "darwin")
    calls = []
    monkeypatch.setattr(
        update_installer, "_trigger_mac_install_and_relaunch", lambda p, c: calls.append((p, c)),
    )

    update_installer.trigger_install_and_relaunch(tmp_path / "Jarvis-x.dmg", "client-123")

    assert calls == [(tmp_path / "Jarvis-x.dmg", "client-123")]


def test_dispatch_calls_windows_trigger_on_win32(monkeypatch, tmp_path):
    monkeypatch.setattr(update_installer.sys, "platform", "win32")
    calls = []
    monkeypatch.setattr(
        update_installer, "_trigger_windows_install_and_relaunch", lambda p, c: calls.append((p, c)),
    )

    update_installer.trigger_install_and_relaunch(tmp_path / "JarvisSetup.exe", "client-123")

    assert calls == [(tmp_path / "JarvisSetup.exe", "client-123")]


def test_dispatch_raises_on_an_unsupported_platform(monkeypatch, tmp_path):
    monkeypatch.setattr(update_installer.sys, "platform", "linux")

    with pytest.raises(RuntimeError, match="isn't supported"):
        update_installer.trigger_install_and_relaunch(tmp_path / "x", None)
