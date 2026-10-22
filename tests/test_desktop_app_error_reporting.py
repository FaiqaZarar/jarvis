"""jarvis.modules.desktop_app.error_reporting - a silent, best-effort
side effect: no dialog, no retry, never surfaces a second error to the
user on top of whatever error they already see. Real run_daemon_call ->
QThreadPool path, matching test_desktop_app_feedback.py's own convention.
"""

from PySide6.QtCore import QThreadPool

from jarvis.modules.cloud import client as cloud_client
from jarvis.modules.cloud import session_store
from jarvis.modules.desktop_app import error_reporting


def test_is_a_silent_no_op_when_no_session_exists(qtbot, monkeypatch):
    monkeypatch.setattr(session_store, "load_session", lambda: None)

    def _must_not_be_called(*a, **k):
        raise AssertionError("report_error should not be called without a session")

    monkeypatch.setattr(cloud_client, "report_error", _must_not_be_called)

    error_reporting.report_error(QThreadPool(), "drive_connect_failed", "access_denied")  # must not raise


def test_sends_a_real_timestamp_and_log_excerpt_when_signed_in(qtbot, monkeypatch):
    monkeypatch.setattr(session_store, "load_session", lambda: ("user@example.com", "tok-abc"))
    monkeypatch.setattr(error_reporting.log_excerpt, "get_recent_log_excerpt", lambda before: "INFO: something happened")
    captured = {}
    monkeypatch.setattr(
        cloud_client, "report_error",
        lambda token, error_type, message, client_timestamp, log_excerpt: captured.update(
            token=token, error_type=error_type, message=message,
            client_timestamp=client_timestamp, log_excerpt=log_excerpt,
        ) or (200, {"reported": True}),
    )

    error_reporting.report_error(QThreadPool(), "drive_connect_failed", "access_denied")

    qtbot.waitUntil(lambda: "token" in captured, timeout=2000)
    assert captured["token"] == "tok-abc"
    assert captured["error_type"] == "drive_connect_failed"
    assert captured["message"] == "access_denied"
    assert captured["client_timestamp"]  # non-empty ISO timestamp
    assert captured["log_excerpt"] == "INFO: something happened"


def test_swallows_a_failure_from_the_report_call_itself(qtbot, monkeypatch):
    """Regression test: a network failure reporting the error must never
    raise back into the caller, or surface a second error dialog on top
    of whatever the user already sees - run_daemon_call's own on_error
    just isn't given here, so a failure is silently dropped."""
    monkeypatch.setattr(session_store, "load_session", lambda: ("user@example.com", "tok-abc"))

    def _boom(*a, **k):
        raise RuntimeError("cloud unreachable")

    monkeypatch.setattr(cloud_client, "report_error", _boom)

    pool = QThreadPool()
    error_reporting.report_error(pool, "drive_connect_failed", "access_denied")  # must not raise
    pool.waitForDone(2000)  # let the worker thread actually run _boom() before the test ends
