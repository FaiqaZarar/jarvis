"""The one gate every outgoing span and event passes through
(docs/observability-DESIGN.md §5). Allow-list, not block-list: a field
that isn't listed here is dropped, so a new call site can't leak a file
name or path by accident - adding a field means editing this file.
"""

import re
from typing import Optional, Sequence

from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult
from opentelemetry.trace import Status

SPAN_NAMES = frozenset({
    "ingest",
    "ingest.reading_files",
    "ingest.processing_documents",
    "ingest.generating_embeddings",
    "ingest.organizing",
    "ingest.reconciling",
})
REDACTED_SPAN_NAME = "redacted"

ATTRIBUTE_KEYS = frozenset({
    "jarvis.connector",
    "jarvis.trigger",
    "jarvis.outcome",
    "jarvis.error_type",
    "jarvis.error_code",
    "jarvis.files.pulled",
    "jarvis.files.failed",
    "jarvis.chunks.created",
    "jarvis.items.total",
    "jarvis.items.processed",
})

EVENT_PROPERTY_KEYS = frozenset({
    # Super properties set on every event (telemetry.init).
    "service",
    "app_version",
    "os",
    # Usage events and error reports (design doc §8).
    "connector",
    "trigger",
    "status",
    "error_code",
    "error_type",
    "phase",
    "duration_s",
    "duration_ms",
    "files_total",
    "files_failed",
    "tool",
    "success",
})

# Second line of defence for string values on allowed keys: identifier-like
# only. A path, sentence or query fails this even if a key is misused.
_SAFE_STRING = re.compile(r"^[A-Za-z0-9_.\-]{1,64}$")
_ABSOLUTE_PATH = re.compile(r"^([A-Za-z]:[\\/]|[\\/])")


def clean_attributes(attributes, allowed_keys=ATTRIBUTE_KEYS) -> dict:
    return {
        key: value
        for key, value in (attributes or {}).items()
        if key in allowed_keys and _is_safe_value(value)
    }


def clean_event(event: dict) -> Optional[dict]:
    """PostHog `before_send` hook. Properties starting with "$" are the
    SDK's own metadata (library name, version, exception details), kept
    once scrubbed; everything else must be allow-listed."""
    properties = event.get("properties") or {}
    event["properties"] = {
        key: value
        for key, value in properties.items()
        if key.startswith("$") or (key in EVENT_PROPERTY_KEYS and _is_safe_value(value))
    }
    for exception in event["properties"].get("$exception_list") or []:
        _scrub_exception(exception)
    return event


def _scrub_exception(exception: dict) -> None:
    # The message can hold paths and file names; the type and our error
    # code carry the meaning.
    exception["value"] = ""
    for frame in (exception.get("stacktrace") or {}).get("frames") or []:
        # Absolute paths include the OS username. Source lines are dropped
        # too: packaged builds ship no source, so they'd only ever show up
        # from a dev checkout, where nobody needs them sent.
        for key in ("abs_path", "context_line", "pre_context", "post_context", "vars"):
            frame.pop(key, None)
        filename = frame.get("filename")
        if isinstance(filename, str) and _ABSOLUTE_PATH.match(filename):
            frame["filename"] = re.split(r"[\\/]", filename)[-1]


def clean_span(span: ReadableSpan) -> ReadableSpan:
    # Events are dropped wholesale: record_exception() puts the exception
    # message and stack trace there, and both can contain paths.
    return ReadableSpan(
        name=span.name if span.name in SPAN_NAMES else REDACTED_SPAN_NAME,
        context=span.context,
        parent=span.parent,
        resource=span.resource,
        attributes=clean_attributes(span.attributes),
        events=(),
        links=span.links,
        kind=span.kind,
        status=Status(span.status.status_code),
        start_time=span.start_time,
        end_time=span.end_time,
        instrumentation_scope=span.instrumentation_scope,
    )


class PrivacyFilteringSpanExporter(SpanExporter):
    def __init__(self, inner: SpanExporter):
        self._inner = inner

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        return self._inner.export([clean_span(span) for span in spans])

    def shutdown(self) -> None:
        self._inner.shutdown()

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return self._inner.force_flush(timeout_millis)


def _is_safe_value(value) -> bool:
    if isinstance(value, bool) or isinstance(value, (int, float)):
        return True
    return isinstance(value, str) and bool(_SAFE_STRING.match(value))
