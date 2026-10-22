"""The privacy filter (docs/observability-DESIGN.md §5) and the ingest
trace. The end-to-end tests run a real sync over files whose folder
names, file names and contents carry recognisable secrets, then check
the exact bytes that would go over the wire to PostHog."""

from pathlib import Path

import pytest
from opentelemetry.exporter.otlp.proto.common.trace_encoder import encode_spans
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExporter, SpanExportResult
from opentelemetry.trace import StatusCode

from jarvis_server import pipeline
from jarvis_server.connectors.base import Connector
from jarvis_server.connectors.filesystem import FilesystemConnector
from jarvis_server.internal import db
from jarvis_server.internal.migrate import run_migrations
from jarvis_server.internal.telemetry.privacy import (
    PrivacyFilteringSpanExporter,
    REDACTED_SPAN_NAME,
    clean_attributes,
    clean_event,
)
from tests.test_pipeline import CyclingLLM, TopicEmbedder

SECRET_FOLDER = "AcmeMergerSecret"
SECRET_FILE = "Layoffs Plan Q4"
SECRET_TEXT = "ZEBRAQUARTZ"
SECRETS = (SECRET_FOLDER, SECRET_FILE, "Layoffs", SECRET_TEXT)


class _WireCapture(SpanExporter):
    """Encodes spans exactly as the OTLP exporter would before sending."""

    def __init__(self):
        self.spans = []
        self.payload = b""

    def export(self, spans):
        self.spans.extend(spans)
        self.payload += encode_spans(spans).SerializeToString()
        return SpanExportResult.SUCCESS

    def shutdown(self):
        pass


@pytest.fixture
def wire(monkeypatch):
    capture = _WireCapture()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(PrivacyFilteringSpanExporter(capture)))
    monkeypatch.setattr(pipeline, "_tracer", provider.get_tracer("test"))
    return capture


@pytest.fixture
def conn(tmp_path: Path):
    path = tmp_path / "privacy.db"
    run_migrations(path)
    return db.get_connection(path)


@pytest.fixture
def secret_corpus(tmp_path: Path) -> Path:
    folder = tmp_path / SECRET_FOLDER
    folder.mkdir()
    topics = {
        "coffee": "Espresso forces hot water through ground coffee; coffee roast matters.",
        "sqlite": "SQLite write-ahead logging lets one SQLite writer and many SQLite readers run.",
        "photosynthesis": "Photosynthesis turns light into glucose; photosynthesis releases oxygen.",
    }
    for topic, text in topics.items():
        for n in (1, 2):
            (folder / f"{SECRET_FILE} {topic} {n}.md").write_text(
                f"# {SECRET_TEXT} {topic}\n\n{SECRET_TEXT} {text}\n"
            )
    return folder


def _assert_no_secrets(capture: _WireCapture) -> None:
    assert capture.payload, "nothing was exported - the test would prove nothing"
    for secret in SECRETS:
        assert secret.encode() not in capture.payload, f"{secret!r} reached the wire"


def test_successful_sync_sends_no_user_data(conn, secret_corpus, tmp_path, wire):
    result = pipeline.run_ingest(
        conn, FilesystemConnector(str(secret_corpus)),
        embedder=TopicEmbedder(), llm_client=CyclingLLM(),
        live_graph_path=tmp_path / "graph.lbdb", trigger="gui",
    )
    assert "organize" in result["hierarchy"]

    _assert_no_secrets(wire)
    names = [span.name for span in wire.spans]
    assert names == [
        "ingest.reading_files",
        "ingest.processing_documents",
        "ingest.generating_embeddings",
        "ingest.organizing",
        "ingest.reconciling",
        "ingest",
    ]
    root = wire.spans[-1]
    assert all(span.parent.span_id == root.context.span_id for span in wire.spans[:-1])
    assert root.attributes["jarvis.connector"] == "filesystem"
    assert root.attributes["jarvis.trigger"] == "gui"
    assert root.attributes["jarvis.outcome"] == "done"
    assert root.attributes["jarvis.files.pulled"] == 6


class _LeakyFailingConnector(Connector):
    name = "filesystem"
    root_id = f"C:/Users/someone/{SECRET_FOLDER}"

    def sync(self, conn, on_progress=None):
        raise PermissionError(f"[Errno 13] Permission denied: 'C:/Users/someone/{SECRET_FOLDER}/{SECRET_FILE}.md'")


def test_failed_sync_records_the_failing_step_but_not_the_message(conn, wire):
    with pytest.raises(PermissionError):
        pipeline.run_ingest(conn, _LeakyFailingConnector(), embedder=TopicEmbedder())

    _assert_no_secrets(wire)
    phase, root = wire.spans
    assert phase.name == "ingest.reading_files"
    for span in (phase, root):
        assert span.status.status_code == StatusCode.ERROR
        assert span.status.description is None
        assert span.attributes["jarvis.outcome"] == "failed"
        assert span.attributes["jarvis.error_type"] == "PermissionError"
        assert span.attributes["jarvis.error_code"] == "unknown"
    assert not pipeline._open_phase_spans


def test_unknown_keys_and_unsafe_values_are_dropped():
    cleaned = clean_attributes({
        "jarvis.connector": "filesystem",
        "jarvis.error_type": "C:\\Users\\someone\\secret.docx",
        "jarvis.trigger": "a sentence with spaces",
        "jarvis.files.pulled": 3,
        "file.path": "/home/someone/secret.md",
        "jarvis.outcome": ["list", "values"],
    })
    assert cleaned == {"jarvis.connector": "filesystem", "jarvis.files.pulled": 3}


def test_unlisted_span_names_and_events_are_scrubbed():
    capture = _WireCapture()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(PrivacyFilteringSpanExporter(capture)))
    tracer = provider.get_tracer("test")

    with tracer.start_as_current_span(f"open {SECRET_FILE}.md") as span:
        span.record_exception(FileNotFoundError(f"/home/someone/{SECRET_FOLDER}/x.md"))
        span.set_status(StatusCode.ERROR, f"failed reading {SECRET_FILE}")

    _assert_no_secrets(capture)
    (exported,) = capture.spans
    assert exported.name == REDACTED_SPAN_NAME
    assert exported.events == ()


def test_event_properties_are_allow_listed():
    event = clean_event({
        "event": "app_opened",
        "properties": {
            "$lib": "posthog-python",
            "service": "jarvis-desktop",
            "os": "win32",
            "query": f"what did {SECRET_FILE} say",
            "app_version": "C:/Users/someone/app",
        },
    })
    assert event["properties"] == {"$lib": "posthog-python", "service": "jarvis-desktop", "os": "win32"}
