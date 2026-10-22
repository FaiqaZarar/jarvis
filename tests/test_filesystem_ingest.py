"""`python -m jarvis.cmd.filesystem_ingest` - the CLI entrypoint.

This module had zero test coverage before the daemon conversion (there
was no test_filesystem_ingest.py at all - confirmed via `git ls-files`),
so there is no pre-existing behavior at risk here; these tests establish
the new daemon-routed contract directly: `main()` sends the folder to
`daemon.client.run_ingest_filesystem` instead of opening a DB connection
and calling `pipeline.run_ingest` in-process, and turns each of the
three daemon-unreachable tiers (docs/local-server-architecture-DESIGN.md
§10) into a clean one-line message instead of a traceback.
"""

import pytest

from jarvis.cmd import filesystem_ingest
from jarvis.modules.daemon.client import DaemonConnectionError, DaemonStartupError, DaemonTimeoutError


def test_main_sends_the_folder_to_the_daemon_and_prints_the_summary(monkeypatch, capsys):
    captured = {}

    def _fake_run_ingest_filesystem(path, *, organize_hierarchy):
        captured.update(path=path, organize_hierarchy=organize_hierarchy)
        return {
            "sync": {"pulled": 2, "fast_skipped": 0, "skipped": 0, "errors": 0},
            "chunks_created": 5,
            "embed": {"newly_embedded": 5},
            "hierarchy": {"skipped": "organize_hierarchy=False"},
        }

    monkeypatch.setattr(filesystem_ingest.daemon_client, "run_ingest_filesystem", _fake_run_ingest_filesystem)

    rc = filesystem_ingest.main(["/some/folder", "--no-organize", "--yes"])

    assert rc == 0
    assert captured == {"path": "/some/folder", "organize_hierarchy": False}
    out = capsys.readouterr().out
    assert "pulled 2" in out and "chunks created:  5" in out


def test_main_confirmation_no_aborts_before_any_ingest(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda *a, **k: "n")

    def _must_not_run(*a, **k):
        raise AssertionError("run_ingest_filesystem called despite the user declining")

    monkeypatch.setattr(filesystem_ingest.daemon_client, "run_ingest_filesystem", _must_not_run)

    assert filesystem_ingest.main(["/some/folder"]) == 0


def test_main_reports_a_daemon_side_ingest_error_without_a_traceback(monkeypatch, capsys):
    """A daemon-side ingest failure (bad path, bad EMBEDDING_API_KEY, ...)
    comes back as a plain dict with an "error" key, not a raised
    exception - a different case from the daemon being unreachable."""
    monkeypatch.setattr(
        filesystem_ingest.daemon_client, "run_ingest_filesystem",
        lambda *a, **k: {"error": {"code": "internal_error", "message": "ValueError: bad path"}},
    )

    rc = filesystem_ingest.main(["/some/folder", "--yes"])

    assert rc == 1
    err = capsys.readouterr().err
    assert "bad path" in err
    assert "Traceback" not in err


@pytest.mark.parametrize("exc_cls,message", [
    (DaemonConnectionError, "The Jarvis background service isn't responding. Try restarting the app."),
    (DaemonTimeoutError, "The Jarvis background service isn't responding. Try restarting the app."),
    (DaemonStartupError, "The Jarvis background service didn't start in time. Try restarting the app."),
])
def test_main_shows_a_clean_message_for_each_daemon_unreachable_tier(monkeypatch, capsys, exc_cls, message):
    def _boom(*a, **k):
        raise exc_cls(message)

    monkeypatch.setattr(filesystem_ingest.daemon_client, "run_ingest_filesystem", _boom)

    rc = filesystem_ingest.main(["/some/folder", "--yes"])

    assert rc == 1
    err = capsys.readouterr().err
    assert message in err
    assert "Traceback" not in err
