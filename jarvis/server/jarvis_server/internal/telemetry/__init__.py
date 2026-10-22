"""Telemetry for both Jarvis processes (the desktop app and the daemon):
OpenTelemetry traces plus PostHog events and exceptions, as designed in
docs/observability-DESIGN.md.

Off unless a PostHog project key is configured (`config.resolve_config`).
Nothing here may raise into the app or block it: sending happens on
background threads, and a failure to start just leaves telemetry off.
"""

import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from posthog import Posthog

from jarvis_server.internal.logging.logging_config import get_logger
from jarvis_server.internal.telemetry.config import resolve_config
from jarvis_server.internal.telemetry.install_id import get_install_id
from jarvis_server.internal.telemetry.privacy import PrivacyFilteringSpanExporter, clean_event

logger = get_logger(__name__)

_EXPORT_TIMEOUT_S = 5
_SHUTDOWN_BUDGET_S = 3.0


@dataclass
class _Session:
    tracer_provider: TracerProvider
    posthog: Posthog
    install_id: str


_session: Optional[_Session] = None


def init(service_name: str, home: Path) -> bool:
    """Start telemetry for this process. Returns whether it is on.
    Safe to call when no key is configured (returns False) and safe to
    call twice (the second call is a no-op)."""
    global _session
    if _session is not None:
        return True
    config = resolve_config()
    if config is None:
        return False
    try:
        install_id = get_install_id(home)
        version = _build_version() or "dev"
        provider = TracerProvider(resource=Resource.create({
            "service.name": service_name,
            "service.version": version,
            "os.type": sys.platform,
            "jarvis.install_id": install_id,
        }))
        provider.add_span_processor(BatchSpanProcessor(PrivacyFilteringSpanExporter(OTLPSpanExporter(
            endpoint=f"{config.host}/i/v1/traces",
            headers={"Authorization": f"Bearer {config.project_key}"},
            timeout=_EXPORT_TIMEOUT_S,
        ))))
        client = Posthog(
            config.project_key,
            host=config.host,
            disable_geoip=True,
            timeout=_EXPORT_TIMEOUT_S,
            enable_exception_autocapture=True,
            # Local variables can hold document text - design doc §5.
            capture_exception_code_variables=False,
            super_properties={"service": service_name, "app_version": version, "os": sys.platform},
            before_send=clean_event,
        )
        trace.set_tracer_provider(provider)
    except Exception:
        logger.warning("Telemetry could not start; continuing without it", exc_info=True)
        return False
    _session = _Session(provider, client, install_id)
    logger.info("Telemetry on for %s", service_name)
    return True


def shutdown() -> None:
    """Flush anything still queued and stop the background senders.
    Call once on clean exit. Gives up after `_SHUTDOWN_BUDGET_S`: offline,
    both senders retry for far longer than anyone should wait to quit."""
    global _session
    session, _session = _session, None
    if session is None:
        return
    stopper = threading.Thread(target=_stop, args=(session,), daemon=True)
    stopper.start()
    stopper.join(_SHUTDOWN_BUDGET_S)


def _stop(session: _Session) -> None:
    for stop in (session.tracer_provider.shutdown, session.posthog.shutdown):
        try:
            stop()
        except Exception:
            logger.warning("Telemetry shutdown step failed", exc_info=True)


def is_enabled() -> bool:
    return _session is not None


def capture(event: str, **properties) -> None:
    """A usage event. Properties not on the privacy allow-list are dropped
    before sending (`privacy.clean_event`)."""
    if _session is None:
        return
    try:
        _session.posthog.capture(event, distinct_id=_session.install_id, properties=properties)
    except Exception:
        logger.warning("Telemetry event %s not sent", event, exc_info=True)


def capture_error(exc: BaseException, *, error_code: str, phase: Optional[str] = None) -> None:
    """Send a handled exception to PostHog error tracking, grouped by error
    code + phase rather than by stack trace (design doc §10), so one real
    problem stays one issue across releases."""
    if _session is None:
        return
    phase = phase or "none"
    try:
        _session.posthog.capture_exception(exc, distinct_id=_session.install_id, properties={
            "error_code": error_code,
            "phase": phase,
            "$exception_fingerprint": f"{error_code}:{phase}",
        })
    except Exception:
        logger.warning("Telemetry error report not sent", exc_info=True)


def get_tracer(name: str) -> trace.Tracer:
    """OpenTelemetry's own no-op tracer while telemetry is off, so call
    sites can create spans unconditionally."""
    return trace.get_tracer(name)


def _build_version() -> Optional[str]:
    # Same VERSION file `jarvis.modules.desktop_app.version` reads;
    # duplicated because jarvis_server can't import the desktop package.
    meipass = getattr(sys, "_MEIPASS", None)
    if not meipass:
        return None
    try:
        return (Path(meipass) / "VERSION").read_text(encoding="utf-8").strip() or None
    except OSError:
        return None
