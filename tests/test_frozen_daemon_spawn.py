"""Frozen-daemon spawn dispatch (docs/desktop-app-DESIGN.md §4, resolved:
single frozen executable, argv-dispatched).

Three independent pieces, each tested directly:

  1. `jarvis.modules.daemon.client._spawn_daemon()` - chooses
     `-m jarvis.cmd.daemon` (unfrozen) or `--daemon` (frozen) as the argv
     it spawns.
  2. `jarvis.cmd.daemon._ensure_fixed_hash_seed()` - the same choice, for
     its own relaunch-with-corrected-env path.
  3. `jarvis.cmd.desktop_app.main()` - the actual dispatch a `--daemon`
     argv is caught by once the frozen binary is running: routes to the
     real daemon entrypoint, not the GUI, and vice versa.

None of this needs an actual PyInstaller build to verify: `sys.frozen`
is just a plain attribute PyInstaller sets on the `sys` module at
runtime (`getattr(sys, "frozen", False)` is the standard, documented way
every piece of code here checks it) - monkeypatching it directly on the
real `sys` module, in-process, exercises the exact same code path a real
frozen build would take, without needing to actually freeze anything.
`sys.frozen` doesn't exist at all under a normal interpreter (confirmed:
`hasattr(sys, "frozen")` is False here), so `monkeypatch.setattr(sys,
"frozen", True, raising=False)` is required - `raising=False` because
monkeypatch's default assumes the attribute already exists.
"""

import os
import subprocess
import sys

import pytest

import jarvis.modules.daemon.client as daemon_client
from jarvis.cmd import daemon as daemon_cmd


@pytest.fixture(autouse=True)
def _no_real_frozen_state(monkeypatch):
    """Belt and suspenders: whatever this test machine's own sys module
    looks like, start every test from a known, explicitly-unfrozen
    baseline, so a leaked real PyInstaller run (or anything else) can't
    make these tests silently pass or fail for the wrong reason."""
    monkeypatch.delattr(sys, "frozen", raising=False)


# ---------------------------------------------------------------------
# 1. daemon_client._spawn_daemon()
# ---------------------------------------------------------------------

def _capture_popen_argv(monkeypatch, tmp_path):
    captured = {}

    def fake_popen(argv, **kwargs):
        captured["argv"] = argv
        class _FakeProc:
            pass
        return _FakeProc()

    monkeypatch.setattr(daemon_client.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(daemon_client, "LOG_FILE", tmp_path / "daemon.log")
    return captured


def test_spawn_daemon_uses_module_flag_when_not_frozen(monkeypatch, tmp_path):
    captured = _capture_popen_argv(monkeypatch, tmp_path)

    daemon_client._spawn_daemon()

    assert captured["argv"] == [sys.executable, "-m", "jarvis.cmd.daemon"]


def test_spawn_daemon_uses_daemon_flag_when_frozen(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    captured = _capture_popen_argv(monkeypatch, tmp_path)

    daemon_client._spawn_daemon()

    assert captured["argv"] == [sys.executable, "--daemon"]


def test_spawn_daemon_still_sets_pythonhashseed_regardless_of_frozen_state(monkeypatch, tmp_path):
    """The frozen/unfrozen branch only changes argv - PYTHONHASHSEED=0
    must still be set in env= either way (docs/organize-checkpointing-
    DESIGN.md §1d/§6 - this is what makes the common auto-start path
    never need daemon.py's own relaunch fallback at all)."""
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    captured_env = {}

    def fake_popen(argv, **kwargs):
        captured_env.update(kwargs.get("env") or {})
        class _FakeProc:
            pass
        return _FakeProc()

    monkeypatch.setattr(daemon_client.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(daemon_client, "LOG_FILE", tmp_path / "daemon.log")

    daemon_client._spawn_daemon()

    assert captured_env["PYTHONHASHSEED"] == "0"


# ---------------------------------------------------------------------
# 2. jarvis.cmd.daemon._ensure_fixed_hash_seed()
# ---------------------------------------------------------------------

def _capture_run_argv(monkeypatch):
    captured = {}

    class _FakeCompletedProcess:
        returncode = 0

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        return _FakeCompletedProcess()

    monkeypatch.setattr(daemon_cmd.subprocess, "run", fake_run)
    return captured


def test_ensure_fixed_hash_seed_relaunches_with_module_flag_when_not_frozen(monkeypatch):
    monkeypatch.delenv("PYTHONHASHSEED", raising=False)
    captured = _capture_run_argv(monkeypatch)

    with pytest.raises(SystemExit):
        daemon_cmd._ensure_fixed_hash_seed()

    assert captured["argv"] == [sys.executable, "-m", "jarvis.cmd.daemon"]


def test_ensure_fixed_hash_seed_relaunches_with_daemon_flag_when_frozen(monkeypatch):
    monkeypatch.delenv("PYTHONHASHSEED", raising=False)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    captured = _capture_run_argv(monkeypatch)

    with pytest.raises(SystemExit):
        daemon_cmd._ensure_fixed_hash_seed()

    assert captured["argv"] == [sys.executable, "--daemon"]


def test_ensure_fixed_hash_seed_does_not_relaunch_when_already_seeded(monkeypatch):
    """Regression guard, frozen or not: if PYTHONHASHSEED is already "0",
    there must be no relaunch at all - confirmed by making subprocess.run
    itself raise if it's ever called."""
    monkeypatch.setenv("PYTHONHASHSEED", "0")
    monkeypatch.setattr(sys, "frozen", True, raising=False)

    def _must_not_be_called(*a, **k):
        raise AssertionError("subprocess.run should not be called when already seeded")

    monkeypatch.setattr(daemon_cmd.subprocess, "run", _must_not_be_called)

    daemon_cmd._ensure_fixed_hash_seed()  # must return normally, not exit


# ---------------------------------------------------------------------
# 3. jarvis.cmd.desktop_app.main() - the actual argv-dispatch a real
#    "--daemon"/"--mcp" invocation is caught by inside the frozen binary
# ---------------------------------------------------------------------

def test_desktop_app_main_routes_to_the_daemon_when_daemon_flag_present(monkeypatch):
    from jarvis.cmd import desktop_app

    calls = []
    monkeypatch.setattr(sys, "argv", ["jarvis.exe", "--daemon"])
    monkeypatch.setattr(daemon_cmd, "_ensure_fixed_hash_seed", lambda: calls.append("seed"))

    import jarvis_server.daemon.server as daemon_server
    monkeypatch.setattr(daemon_server, "main", lambda: calls.append("daemon_main"))

    desktop_app.main()

    assert calls == ["seed", "daemon_main"]


def test_desktop_app_main_routes_to_the_gui_when_daemon_flag_absent(monkeypatch):
    from jarvis.cmd import desktop_app

    calls = []
    monkeypatch.setattr(sys, "argv", ["jarvis.exe"])

    import jarvis.modules.desktop_app.app as gui_app
    monkeypatch.setattr(gui_app, "main", lambda: calls.append("gui_main"))

    desktop_app.main()

    assert calls == ["gui_main"]


def test_desktop_app_main_routes_to_mcp_when_mcp_flag_present(monkeypatch):
    from jarvis.cmd import desktop_app

    calls = []
    monkeypatch.setattr(sys, "argv", ["jarvis.exe", "--mcp"])

    import jarvis.cmd.mcp_server as mcp_server
    monkeypatch.setattr(mcp_server, "main", lambda: calls.append("mcp_main"))

    desktop_app.main()

    assert calls == ["mcp_main"]


def test_desktop_app_main_defaults_daemon_home_for_mcp_when_unset(monkeypatch):
    """--mcp can auto-spawn the daemon with nothing else having run first
    (no GUI to have already set JARVIS_DAEMON_HOME) - regression guard for
    the real frozen-build failure this default fixes (see module
    docstring in jarvis/cmd/desktop_app.py)."""
    from jarvis.cmd import desktop_app

    monkeypatch.setattr(sys, "argv", ["jarvis.exe", "--mcp"])
    monkeypatch.delenv("JARVIS_DAEMON_HOME", raising=False)

    import jarvis_server.internal.env_config as env_config
    monkeypatch.setattr(env_config, "default_daemon_home", lambda: "C:\\fake\\Jarvis\\Jarvis")

    import jarvis.cmd.mcp_server as mcp_server
    monkeypatch.setattr(mcp_server, "main", lambda: None)

    desktop_app.main()

    assert os.environ["JARVIS_DAEMON_HOME"] == "C:\\fake\\Jarvis\\Jarvis"


def test_desktop_app_main_does_not_override_daemon_home_for_mcp_when_already_set(monkeypatch):
    from jarvis.cmd import desktop_app

    monkeypatch.setattr(sys, "argv", ["jarvis.exe", "--mcp"])
    monkeypatch.setenv("JARVIS_DAEMON_HOME", "C:\\already\\set")

    import jarvis_server.internal.env_config as env_config
    monkeypatch.setattr(env_config, "default_daemon_home", lambda: "C:\\fake\\Jarvis\\Jarvis")

    import jarvis.cmd.mcp_server as mcp_server
    monkeypatch.setattr(mcp_server, "main", lambda: None)

    desktop_app.main()

    assert os.environ["JARVIS_DAEMON_HOME"] == "C:\\already\\set"


def test_desktop_app_main_defaults_daemon_home_for_gui_when_unset(monkeypatch):
    """Regression guard for a real, confirmed frozen-build failure
    ("Jarvis background service didn't start in time", 100% reproducible,
    even though the daemon's own log showed it starting successfully
    every time): the GUI branch must set JARVIS_DAEMON_HOME the same way
    --mcp does, and before its own gui_main import - see
    jarvis/cmd/desktop_app.py's module docstring for the full story.
    This same-process test only proves the dispatch calls
    _ensure_daemon_home_default() at the right point in its own control
    flow; see test_gui_dispatch_sets_daemon_home_before_client_module_is_
    ever_imported below for why a same-process test can't fully prove the
    real bug is fixed (jarvis.modules.daemon.client's own module-level
    caching means the meaningful check needs a fresh interpreter)."""
    from jarvis.cmd import desktop_app

    monkeypatch.setattr(sys, "argv", ["jarvis.exe"])
    monkeypatch.delenv("JARVIS_DAEMON_HOME", raising=False)

    import jarvis_server.internal.env_config as env_config
    monkeypatch.setattr(env_config, "default_daemon_home", lambda: "C:\\fake\\Jarvis\\Jarvis")

    import jarvis.modules.desktop_app.app as gui_app
    monkeypatch.setattr(gui_app, "main", lambda: None)

    desktop_app.main()

    assert os.environ["JARVIS_DAEMON_HOME"] == "C:\\fake\\Jarvis\\Jarvis"


def test_gui_dispatch_sets_daemon_home_before_client_module_is_ever_imported(tmp_path):
    """The exact root cause of the real bug above: JARVIS_DAEMON_HOME used
    to be set *inside* jarvis.modules.desktop_app.app.main()'s body - but
    that module's own top-level `from jarvis.modules.desktop_app.
    main_window import MainWindow` transitively imports jarvis.modules.
    daemon.client, whose _DAEMON_HOME/DISCOVERY_FILE/LOG_FILE are plain
    module-level statements, computed once and cached at *import* time.
    That transitive import happens the moment app.py itself is imported -
    before main()'s body, and whatever it sets, ever runs. A same-process
    test can't catch this regressing (client is already cached from
    earlier tests in this same suite) - only a fresh interpreter can
    genuinely prove "which env var value was in effect before this
    import happened", so this spawns one."""
    script = (
        "import os\n"
        "os.environ.pop('JARVIS_DAEMON_HOME', None)\n"
        f"os.environ['APPDATA'] = {str(tmp_path)!r}\n"
        "from jarvis.cmd.desktop_app import _ensure_daemon_home_default\n"
        "_ensure_daemon_home_default()\n"
        "import jarvis.modules.daemon.client as client\n"
        "expected = os.environ['JARVIS_DAEMON_HOME']\n"
        "assert str(client._DAEMON_HOME) == expected, (str(client._DAEMON_HOME), expected)\n"
        "print('OK')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(daemon_client.REPO_ROOT), capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "OK" in result.stdout


def test_client_module_caches_daemon_home_at_import_time_not_per_call(tmp_path):
    """Documents *why* the ordering above matters: importing client.py
    before the env var is set permanently bakes in the wrong path for
    this process's whole lifetime, even if the env var is set correctly
    moments later - the exact trap the real GUI bug fell into."""
    script = (
        "import os\n"
        "os.environ.pop('JARVIS_DAEMON_HOME', None)\n"
        "import jarvis.modules.daemon.client as client\n"  # imported before the env var is set
        "stale = str(client._DAEMON_HOME)\n"
        f"os.environ['JARVIS_DAEMON_HOME'] = {str(tmp_path)!r}\n"
        "assert str(client._DAEMON_HOME) == stale, 'must NOT have picked up the late env var'\n"
        f"assert str(client._DAEMON_HOME) != {str(tmp_path)!r}\n"
        "print('OK')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(daemon_client.REPO_ROOT), capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "OK" in result.stdout
