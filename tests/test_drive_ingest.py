"""`python -m jarvis.cmd.drive_ingest` - the CLI entrypoint + folder picker.

Reuses the Drive service stub from test_drive_connector (same pattern
test_pipeline.py already uses). No real API calls, no real ingest.
"""

import pytest

from jarvis.cmd import drive_ingest
from jarvis_server.connectors import drive
from tests.test_drive_connector import _FakeDriveService, _http_error


def _feed_input(monkeypatch, *responses):
    """Make builtins.input() return each of `responses` in turn."""
    it = iter(responses)
    monkeypatch.setattr("builtins.input", lambda *a, **k: next(it))


# ---------------------------------------------------------------------
# pick_folder
# ---------------------------------------------------------------------

def test_pick_folder_selects_a_top_level_folder_by_number_then_zero(monkeypatch, capsys):
    service = _FakeDriveService(folders_by_parent={
        "root": [{"id": "docs", "name": "Documents"}, {"id": "pics", "name": "Photos"}],
        "docs": [],
    })
    # 1 -> open "Documents";  0 -> ingest "Documents"
    _feed_input(monkeypatch, "1", "0")

    folder_id, name = drive_ingest.pick_folder(service)

    assert (folder_id, name) == ("docs", "Documents")
    out = capsys.readouterr().out
    assert "1. Documents" in out and "2. Photos" in out


def test_pick_folder_can_navigate_into_a_subfolder_and_back(monkeypatch, capsys):
    service = _FakeDriveService(folders_by_parent={
        "root": [{"id": "docs", "name": "Documents"}],
        "docs": [{"id": "inv", "name": "Invoices"}, {"id": "notes", "name": "Notes"}],
        "inv": [],
    })
    # open Documents -> open Invoices -> "b" back to Documents -> open Notes -> select Notes
    _feed_input(monkeypatch, "1", "1", "b", "2", "0")

    folder_id, name = drive_ingest.pick_folder(service)

    assert (folder_id, name) == ("notes", "Notes")
    out = capsys.readouterr().out
    assert "Location: My Drive / Documents / Invoices" in out
    assert "Location: My Drive / Documents / Notes" in out


def test_pick_folder_zero_at_root_ingests_my_drive_top_level(monkeypatch):
    service = _FakeDriveService(folders_by_parent={"root": [{"id": "x", "name": "X"}]})
    _feed_input(monkeypatch, "0")

    folder_id, name = drive_ingest.pick_folder(service)

    assert (folder_id, name) == ("root", "My Drive")


def test_pick_folder_reprompts_on_garbage_and_out_of_range(monkeypatch, capsys):
    service = _FakeDriveService(folders_by_parent={"root": [{"id": "a", "name": "A"}], "a": []})
    _feed_input(monkeypatch, "banana", "9", "1", "0")

    folder_id, _ = drive_ingest.pick_folder(service)

    assert folder_id == "a"
    out = capsys.readouterr().out
    assert "Please enter a number" in out
    assert "between 0 and 1" in out


def test_pick_folder_rate_limited_listing_raises_a_clear_runtimeerror(monkeypatch):
    service = _FakeDriveService(list_exc=_http_error(429, "Too Many Requests"))

    with pytest.raises(RuntimeError, match="rate-limiting"):
        drive_ingest.pick_folder(service)


_FOLDER = "application/vnd.google-apps.folder"
_GDOC = "application/vnd.google-apps.document"


def test_pick_folder_reports_the_real_recursive_file_count_before_confirming(monkeypatch, capsys):
    """The user picks 'Reports' (no direct-child files, so the old picker
    would have looked empty) - but it has two sub-folders with real
    content underneath. The pre-confirmation count must reflect the whole
    sub-tree, not the (zero) direct children."""
    service = _FakeDriveService(
        folders_by_parent={
            "root": [{"id": "reports", "name": "Reports"}],
            "reports": [{"id": "q1", "name": "Q1"}, {"id": "q2", "name": "Q2"}],
            "q1": [], "q2": [],
        },
        files_by_parent={
            "reports": [
                {"id": "q1", "name": "Q1", "mimeType": _FOLDER},
                {"id": "q2", "name": "Q2", "mimeType": _FOLDER},
            ],
            "q1": [
                {"id": "a", "name": "a", "mimeType": _GDOC, "modifiedTime": "t"},
                {"id": "b", "name": "b", "mimeType": _GDOC, "modifiedTime": "t"},
            ],
            "q2": [
                {"id": "c", "name": "c", "mimeType": _GDOC, "modifiedTime": "t"},
                {"id": "readme", "name": "readme", "mimeType": "image/png", "modifiedTime": "t"},
            ],
        },
    )
    _feed_input(monkeypatch, "1", "0")   # open Reports, then select it

    folder_id, name = drive_ingest.pick_folder(service)

    assert (folder_id, name) == ("reports", "Reports")
    out = capsys.readouterr().out
    assert "4 item(s) found, 3 directly ingestable" in out   # a,b,c,readme  /  a,b,c


# ---------------------------------------------------------------------
# main()
# ---------------------------------------------------------------------

def test_main_with_folder_id_flag_skips_picker_and_runs_ingest(monkeypatch, capsys):
    service = _FakeDriveService()
    monkeypatch.setattr(drive_ingest.drive, "get_drive_service", lambda *a, **k: service)

    captured = {}

    def _fake_run_ingest_drive(folder_id, *, organize_hierarchy):
        captured.update(folder_id=folder_id, organize_hierarchy=organize_hierarchy)
        return {
            "sync": {"pulled": 3, "fast_skipped": 0, "skipped": 1, "errors": 0},
            "chunks_created": 7,
            "embed": {"newly_embedded": 7},
            "hierarchy": {"skipped": "organize_hierarchy=False"},
        }

    monkeypatch.setattr(drive_ingest.daemon_client, "run_ingest_drive", _fake_run_ingest_drive)

    rc = drive_ingest.main(["--folder-id", "abc123", "--no-organize"])

    assert rc == 0
    assert captured["folder_id"] == "abc123"
    assert captured["organize_hierarchy"] is False
    out = capsys.readouterr().out
    assert "pulled 3" in out and "chunks created:  7" in out


def test_main_uses_the_picker_when_no_folder_id_given(monkeypatch):
    service = _FakeDriveService(folders_by_parent={"root": [{"id": "f1", "name": "Reports"}], "f1": []})
    monkeypatch.setattr(drive_ingest.drive, "get_drive_service", lambda *a, **k: service)
    # picker: open "Reports" -> select it;  then the confirm prompt -> "y"
    _feed_input(monkeypatch, "1", "0", "y")

    seen = {}

    def _fake_run_ingest_drive(folder_id, **k):
        seen["folder_id"] = folder_id
        return {
            "sync": {"pulled": 0, "fast_skipped": 0, "skipped": 0, "errors": 0},
            "chunks_created": 0, "embed": {}, "hierarchy": {"skipped": "x"},
        }

    monkeypatch.setattr(drive_ingest.daemon_client, "run_ingest_drive", _fake_run_ingest_drive)

    rc = drive_ingest.main([])

    assert rc == 0
    assert seen["folder_id"] == "f1"


def test_main_confirmation_no_aborts_before_any_ingest(monkeypatch):
    service = _FakeDriveService(folders_by_parent={"root": [{"id": "f1", "name": "Reports"}], "f1": []})
    monkeypatch.setattr(drive_ingest.drive, "get_drive_service", lambda *a, **k: service)
    _feed_input(monkeypatch, "0", "n")   # select My Drive root, then decline the confirm

    def _must_not_run(*a, **k):
        raise AssertionError("run_ingest_drive called despite the user declining")

    monkeypatch.setattr(drive_ingest.daemon_client, "run_ingest_drive", _must_not_run)

    assert drive_ingest.main([]) == 0


def test_main_turns_a_runtimeerror_into_a_clean_message_not_a_traceback(monkeypatch, capsys):
    def _boom(*a, **k):
        raise RuntimeError(
            "GOOGLE_OAUTH_CLIENT_ID / GOOGLE_OAUTH_CLIENT_SECRET are not set."
        )

    monkeypatch.setattr(drive_ingest.drive, "get_drive_service", _boom)

    rc = drive_ingest.main(["--folder-id", "x"])

    assert rc == 1
    err = capsys.readouterr().err
    assert "GOOGLE_OAUTH_CLIENT_ID" in err
    assert "Traceback" not in err


def test_main_daemon_connection_error_shows_clean_message(monkeypatch, capsys):
    """Tier 2 (design doc §10): the daemon was reachable a moment ago but
    the actual ingest request failed outright, even after the built-in
    retry - main() must show the calm message, not a traceback."""
    service = _FakeDriveService()
    monkeypatch.setattr(drive_ingest.drive, "get_drive_service", lambda *a, **k: service)

    def _boom(*a, **k):
        raise drive_ingest.DaemonConnectionError(
            "The Jarvis background service isn't responding. Try restarting the app."
        )

    monkeypatch.setattr(drive_ingest.daemon_client, "run_ingest_drive", _boom)

    rc = drive_ingest.main(["--folder-id", "abc123", "--yes"])

    assert rc == 1
    err = capsys.readouterr().err
    assert "isn't responding" in err
    assert "Traceback" not in err


def test_main_daemon_timeout_error_shows_clean_message(monkeypatch, capsys):
    """Tier 3 (design doc §10): same as above, but specifically a timeout."""
    service = _FakeDriveService()
    monkeypatch.setattr(drive_ingest.drive, "get_drive_service", lambda *a, **k: service)

    def _boom(*a, **k):
        raise drive_ingest.DaemonTimeoutError(
            "The Jarvis background service isn't responding. Try restarting the app."
        )

    monkeypatch.setattr(drive_ingest.daemon_client, "run_ingest_drive", _boom)

    rc = drive_ingest.main(["--folder-id", "abc123", "--yes"])

    assert rc == 1
    err = capsys.readouterr().err
    assert "isn't responding" in err
    assert "Traceback" not in err


def test_main_daemon_startup_error_shows_clean_message(monkeypatch, capsys):
    """Tier 1: auto-start itself failed. DaemonStartupError is a
    RuntimeError subclass, so it's also covered by the existing
    RuntimeError-handling test above, but this checks it by name too,
    since that subclassing detail is exactly the kind of thing a future
    refactor could break silently."""
    service = _FakeDriveService()
    monkeypatch.setattr(drive_ingest.drive, "get_drive_service", lambda *a, **k: service)

    def _boom(*a, **k):
        raise drive_ingest.daemon_client.DaemonStartupError(
            "The Jarvis background service didn't start in time. Try restarting the app."
        )

    monkeypatch.setattr(drive_ingest.daemon_client, "run_ingest_drive", _boom)

    rc = drive_ingest.main(["--folder-id", "abc123", "--yes"])

    assert rc == 1
    err = capsys.readouterr().err
    assert "didn't start in time" in err
    assert "Traceback" not in err
