"""Error classification (jarvis_server.internal.errors), the daemon's
ingest error envelope, and what an error report sends to PostHog."""

import json
import socket

import pytest
import requests
from opentelemetry.sdk.trace import TracerProvider
from posthog import Posthog

import jarvis_server.daemon.server as daemon_server
import jarvis_server.pipeline as pipeline_mod
from jarvis_server.internal import telemetry
from jarvis_server.internal.errors import (
    USER_MESSAGES,
    DriveAuthRequired,
    EmbeddingAuthError,
    ErrorCode,
    RateLimitedError,
    classify,
)
from jarvis_server.internal.telemetry.privacy import clean_event
from tests.test_daemon import _ingest, client, db_path, route_db  # noqa: F401 - fixtures


def _http_error(status: int) -> requests.HTTPError:
    response = requests.Response()
    response.status_code = status
    return requests.HTTPError(response=response)


class _GoogleHttpError(Exception):
    """Shaped like googleapiclient.errors.HttpError: status on `.resp`."""

    def __init__(self, status):
        self.resp = type("Resp", (), {"status": status})()


class RefreshError(Exception):
    """Same name as google.auth.exceptions.RefreshError."""


@pytest.mark.parametrize("exc, phase, expected", [
    (EmbeddingAuthError("401"), "generating_embeddings", ErrorCode.EMBEDDING_KEY_INVALID),
    (DriveAuthRequired("sign in"), "reading_files", ErrorCode.DRIVE_AUTH_EXPIRED),
    (RefreshError("revoked"), "reading_files", ErrorCode.DRIVE_AUTH_EXPIRED),
    (RateLimitedError("slow down"), "reading_files", ErrorCode.RATE_LIMITED),
    (_http_error(429), "generating_embeddings", ErrorCode.RATE_LIMITED),
    (_GoogleHttpError(429), "reading_files", ErrorCode.RATE_LIMITED),
    (requests.ConnectionError("reset"), "generating_embeddings", ErrorCode.NETWORK_UNAVAILABLE),
    (socket.gaierror("no dns"), "reading_files", ErrorCode.NETWORK_UNAVAILABLE),
    (ValueError("bad label"), "organizing", ErrorCode.ORGANIZE_FAILED),
    (ValueError("swap failed"), "reconciling", ErrorCode.ORGANIZE_FAILED),
    (ValueError("??"), "reading_files", ErrorCode.UNKNOWN),
    (_http_error(500), None, ErrorCode.UNKNOWN),
])
def test_classify(exc, phase, expected):
    assert classify(exc, phase) is expected


def test_classify_follows_the_cause_chain():
    try:
        try:
            raise requests.ConnectionError("reset by peer")
        except requests.ConnectionError as cause:
            raise RuntimeError("embedding failed") from cause
    except RuntimeError as wrapper:
        assert classify(wrapper, "generating_embeddings") is ErrorCode.NETWORK_UNAVAILABLE


def test_typed_errors_stay_runtime_errors_for_existing_callers():
    for cls in (EmbeddingAuthError, DriveAuthRequired, RateLimitedError):
        assert issubclass(cls, RuntimeError)


def test_every_code_has_a_user_message():
    assert set(USER_MESSAGES) == set(ErrorCode)
    assert all("Error" not in message for message in USER_MESSAGES.values())


def test_failed_ingest_returns_a_code_and_calm_message_and_reports_it(client, monkeypatch):
    secret_path = "C:/Users/someone/AcmeMergerSecret/plan.md"
    events, errors = [], []
    monkeypatch.setattr(daemon_server.telemetry, "capture", lambda event, **p: events.append((event, p)))
    monkeypatch.setattr(
        daemon_server.telemetry, "capture_error", lambda exc, **kw: errors.append((type(exc), kw)),
    )

    def failing_ingest(connector, organize_hierarchy):
        daemon_server._ingest_status["phase"] = pipeline_mod.PHASE_GENERATING_EMBEDDINGS
        raise EmbeddingAuthError(f"rejected while embedding {secret_path}")

    monkeypatch.setattr(daemon_server, "_run_ingest_for", failing_ingest)
    outcome = _ingest(client, {"path": "/tmp/x", "organize_hierarchy": False})

    assert outcome == {"error": {
        "code": "embedding_key_invalid",
        "message": USER_MESSAGES[ErrorCode.EMBEDDING_KEY_INVALID],
    }}
    assert errors == [(EmbeddingAuthError, {"error_code": "embedding_key_invalid", "phase": "generating_embeddings"})]
    assert [e for e, _ in events] == ["sync_started", "sync_finished"]
    finished = events[1][1]
    assert finished["status"] == "failed"
    assert finished["error_code"] == "embedding_key_invalid"
    assert finished["connector"] == "filesystem"


@pytest.fixture
def sent_events(monkeypatch):
    """A real PostHog client that never sends: records each event exactly
    as it would leave the machine (after the privacy hook)."""
    sent = []
    client = Posthog(
        "phc_test", host="http://127.0.0.1:9", send=False,
        capture_exception_code_variables=False,
        before_send=lambda event: sent.append(clean_event(event)) or None,
    )
    monkeypatch.setattr(telemetry, "_session", telemetry._Session(TracerProvider(), client, "install-1"))
    yield sent
    client.shutdown()


def test_error_report_is_grouped_by_code_and_phase_and_carries_no_paths(sent_events):
    try:
        open("C:/Users/someone/AcmeMergerSecret/Layoffs Plan.md")
    except OSError as exc:
        telemetry.capture_error(exc, error_code="unknown", phase="reading_files")

    (event,) = sent_events
    props = event["properties"]
    assert props["$exception_fingerprint"] == "unknown:reading_files"
    assert props["error_code"] == "unknown"
    assert props["phase"] == "reading_files"
    assert props["$exception_list"][0]["type"] == "FileNotFoundError"
    wire = json.dumps(event, default=str)
    for secret in ("AcmeMergerSecret", "Layoffs", "someone"):
        assert secret not in wire
    frames = props["$exception_list"][0]["stacktrace"]["frames"]
    assert all("abs_path" not in frame for frame in frames)


def test_usage_event_drops_unlisted_properties(sent_events):
    telemetry.capture("sync_finished", connector="drive", status="done", folder="AcmeMergerSecret")

    (event,) = sent_events
    assert event["event"] == "sync_finished"
    assert event["distinct_id"] == "install-1"
    assert "folder" not in event["properties"]
    assert event["properties"]["connector"] == "drive"


def test_unreadable_files_are_stored_against_the_run(tmp_path, monkeypatch):
    from jarvis_server.connectors.filesystem import FilesystemConnector
    from jarvis_server.connectors.filesystem import sync as fs_sync
    from jarvis_server.internal import db
    from jarvis_server.internal.migrate import run_migrations
    from tests.test_pipeline import StubEmbedder

    folder = tmp_path / "notes"
    folder.mkdir()
    (folder / "good.md").write_text("# Fine\n\nreadable text\n")
    (folder / "Broken Contract.md").write_text("# Broken\n\nunreadable\n")
    real_open = fs_sync.open_file
    monkeypatch.setattr(
        fs_sync, "open_file",
        lambda path: (None, "simulated") if path.name.startswith("Broken") else real_open(path),
    )
    db_path = tmp_path / "t.db"
    run_migrations(db_path)
    conn = db.get_connection(db_path)

    result = pipeline_mod.run_ingest(
        conn, FilesystemConnector(str(folder)), embedder=StubEmbedder(), organize_hierarchy=False,
    )

    assert result["sync"]["errors"] == 1
    assert "failed_files" not in result["sync"]
    (row,) = db.get_ingest_run_file_errors(conn, result["run_id"])
    assert row["display_name"] == "Broken Contract.md"
    assert row["source_id"].endswith("Broken Contract.md")
    assert row["error_code"] == "file_read_failed"


def test_a_failed_run_records_why(tmp_path):
    from jarvis_server.connectors.base import Connector
    from jarvis_server.internal import db
    from jarvis_server.internal.migrate import run_migrations
    from tests.test_pipeline import StubEmbedder

    class _ExpiredDrive(Connector):
        name = "drive"
        root_id = "folder-1"

        def sync(self, conn, on_progress=None):
            raise DriveAuthRequired("needs sign-in")

    db_path = tmp_path / "t.db"
    run_migrations(db_path)
    conn = db.get_connection(db_path)
    with pytest.raises(DriveAuthRequired):
        pipeline_mod.run_ingest(conn, _ExpiredDrive(), embedder=StubEmbedder())

    (run,) = db.get_recent_ingest_runs(conn)
    assert run["status"] == db.RUN_STATUS_FAILED
    assert run["error_code"] == "drive_auth_expired"
