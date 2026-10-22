"""Request metrics + GET /metrics (issue #145's Prometheus scrape
target). One MeterProvider for the whole process, backed by
opentelemetry-exporter-prometheus's PrometheusMetricReader, which mirrors
every recorded metric into prometheus_client's default registry -
`generate_latest()` is what /metrics actually serves.
"""

import time

from opentelemetry.exporter.prometheus import PrometheusMetricReader
from opentelemetry.sdk.metrics import MeterProvider
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

_reader = PrometheusMetricReader()
_provider = MeterProvider(metric_readers=[_reader])
_meter = _provider.get_meter("jarvis_cloud_server")

request_counter = _meter.create_counter(
    "http_requests_total", description="Total HTTP requests handled, by route and status.",
)
request_latency = _meter.create_histogram(
    "http_request_duration_seconds", unit="s",
    description="Request latency in seconds, by route.",
)
# Incremented once per successful /auth/verify - what DAU tracking is
# meant to run on (issue #145), not inferred from raw request volume.
login_counter = _meter.create_counter(
    "logins_total", description="Successful magic-link sign-ins.",
)


class MetricsMiddleware(BaseHTTPMiddleware):
    """Records request_counter/request_latency for every request except
    /metrics itself - scraping the scrape target would otherwise inflate
    its own numbers every time Prometheus polls it."""

    async def dispatch(self, request, call_next):
        if request.url.path == "/metrics":
            return await call_next(request)
        start = time.monotonic()
        response = await call_next(request)
        duration = time.monotonic() - start
        attributes = {"route": request.url.path, "status": str(response.status_code)}
        request_counter.add(1, attributes)
        request_latency.record(duration, attributes)
        return response


async def metrics_endpoint(request):
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
