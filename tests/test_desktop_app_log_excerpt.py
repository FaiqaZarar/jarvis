"""jarvis.modules.desktop_app.log_excerpt - window selection against a
real file on disk (no mocking the file read - the whole point is
exercising the tail-seek + timestamp-parsing logic for real), and the
"never raises" contract for a missing file."""

from datetime import datetime

from jarvis.modules.desktop_app import log_excerpt


def _write_log(path, lines):
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_returns_empty_string_when_the_log_file_is_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(log_excerpt, "LOG_FILE", tmp_path / "does_not_exist.log")

    result = log_excerpt.get_recent_log_excerpt(datetime(2026, 1, 1, 12, 0, 0))

    assert result == ""


def test_selects_only_lines_within_the_window(monkeypatch, tmp_path):
    log_path = tmp_path / "jarvis_daemon.log"
    _write_log(log_path, [
        "2026-01-01 11:55:00,000 INFO daemon: too old, outside the window",
        "2026-01-01 11:58:30,000 INFO daemon: inside the window",
        "2026-01-01 11:59:59,000 INFO daemon: right at the edge",
        "2026-01-01 12:00:00,000 INFO daemon: exactly at the click",
    ])
    monkeypatch.setattr(log_excerpt, "LOG_FILE", log_path)

    result = log_excerpt.get_recent_log_excerpt(datetime(2026, 1, 1, 12, 0, 0), minutes=2)

    assert "too old" not in result
    assert "inside the window" in result
    assert "right at the edge" in result
    assert "exactly at the click" in result


def test_keeps_a_continuation_line_following_an_in_window_entry(monkeypatch, tmp_path):
    log_path = tmp_path / "jarvis_daemon.log"
    _write_log(log_path, [
        "2026-01-01 11:59:00,000 ERROR daemon: request failed",
        "Traceback (most recent call last):",
        "  ValueError: boom",
    ])
    monkeypatch.setattr(log_excerpt, "LOG_FILE", log_path)

    result = log_excerpt.get_recent_log_excerpt(datetime(2026, 1, 1, 12, 0, 0), minutes=2)

    assert "request failed" in result
    assert "ValueError: boom" in result


def test_drops_a_continuation_line_following_an_out_of_window_entry(monkeypatch, tmp_path):
    log_path = tmp_path / "jarvis_daemon.log"
    _write_log(log_path, [
        "2026-01-01 11:00:00,000 ERROR daemon: an old failure",
        "  ValueError: this stack trace belongs to the old entry",
        "2026-01-01 11:59:30,000 INFO daemon: a real in-window line",
    ])
    monkeypatch.setattr(log_excerpt, "LOG_FILE", log_path)

    result = log_excerpt.get_recent_log_excerpt(datetime(2026, 1, 1, 12, 0, 0), minutes=2)

    assert "old failure" not in result
    assert "this stack trace belongs to the old entry" not in result
    assert "a real in-window line" in result


def test_truncates_to_max_chars_keeping_the_most_recent_content(monkeypatch, tmp_path):
    log_path = tmp_path / "jarvis_daemon.log"
    _write_log(log_path, [
        "2026-01-01 11:59:00,000 INFO daemon: " + ("a" * 50),
        "2026-01-01 11:59:30,000 INFO daemon: " + ("b" * 50),
    ])
    monkeypatch.setattr(log_excerpt, "LOG_FILE", log_path)

    result = log_excerpt.get_recent_log_excerpt(datetime(2026, 1, 1, 12, 0, 0), minutes=2, max_chars=40)

    assert len(result) == 40
    assert "b" * 40 in result or "bbbb" in result
    assert "a" * 40 not in result
