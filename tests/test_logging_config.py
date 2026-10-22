"""jarvis_server.internal.logging.logging_config - log location and
startup rotation."""

from pathlib import Path

from jarvis_server.internal.logging import logging_config


def test_windows_logs_go_to_local_app_data(tmp_path, monkeypatch):
    monkeypatch.setattr(logging_config.sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert logging_config.default_log_dir() == tmp_path / "Jarvis" / "Logs"
    assert (tmp_path / "Jarvis" / "Logs").is_dir()


def test_macos_logs_go_to_library_logs(tmp_path, monkeypatch):
    monkeypatch.setattr(logging_config.sys, "platform", "darwin")
    monkeypatch.setattr(logging_config.Path, "home", lambda: tmp_path)
    assert logging_config.default_log_dir() == tmp_path / "Library" / "Logs" / "Jarvis"


def test_linux_logs_follow_xdg_state_home(tmp_path, monkeypatch):
    monkeypatch.setattr(logging_config.sys, "platform", "linux")
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    assert logging_config.default_log_dir() == tmp_path / "jarvis"


def _write(path: Path, size: int, marker: str) -> None:
    path.write_text(marker + "x" * (size - len(marker)))


def test_small_log_is_left_alone(tmp_path):
    log = tmp_path / "jarvis_daemon.log"
    _write(log, 10, "current")
    logging_config.rotate_if_large(log, max_bytes=100)
    assert log.read_text().startswith("current")
    assert not (tmp_path / "jarvis_daemon.log.1").exists()


def test_large_log_rotates_and_the_oldest_backup_is_dropped(tmp_path):
    log = tmp_path / "jarvis_daemon.log"
    _write(log, 200, "current")
    _write(tmp_path / "jarvis_daemon.log.1", 10, "one")
    _write(tmp_path / "jarvis_daemon.log.2", 10, "two")
    _write(tmp_path / "jarvis_daemon.log.3", 10, "three")

    logging_config.rotate_if_large(log, max_bytes=100, backups=3)

    assert not log.exists()
    assert (tmp_path / "jarvis_daemon.log.1").read_text().startswith("current")
    assert (tmp_path / "jarvis_daemon.log.2").read_text().startswith("one")
    assert (tmp_path / "jarvis_daemon.log.3").read_text().startswith("two")


def test_missing_log_is_not_an_error(tmp_path):
    logging_config.rotate_if_large(tmp_path / "nope.log", max_bytes=1)
