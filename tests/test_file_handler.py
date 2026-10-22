"""jarvis_server.internal.file_handler.reader - generic file I/O, no per-type
parsing knowledge."""

from pathlib import Path

from jarvis_server.internal.file_handler.reader import open_file


def test_open_file_reads_bytes_and_lowercases_extension(tmp_path: Path):
    target = tmp_path / "Note.TXT"
    target.write_bytes(b"hello")

    opened, error = open_file(target)

    assert error is None
    assert opened.raw_bytes == b"hello"
    assert opened.extension == ".txt"
    assert opened.path == target


def test_open_file_missing_file_returns_error_not_exception(tmp_path: Path):
    missing = tmp_path / "does_not_exist.txt"

    opened, error = open_file(missing)

    assert opened is None
    assert error is not None


def test_open_file_permission_error_is_actionable(tmp_path: Path, monkeypatch):
    target = tmp_path / "blocked.txt"
    target.write_bytes(b"data")

    def raise_permission_error(self):
        raise PermissionError(13, "Permission denied", str(target))

    monkeypatch.setattr(Path, "read_bytes", raise_permission_error)

    opened, error = open_file(target)

    assert opened is None
    assert "Full Disk Access" in error
