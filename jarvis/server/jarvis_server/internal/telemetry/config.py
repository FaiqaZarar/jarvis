import os
from dataclasses import dataclass
from typing import Optional

from jarvis_server.internal import _bundled_credentials
from jarvis_server.internal.env_config import telemetry_enabled

DEFAULT_HOST = "https://eu.i.posthog.com"


@dataclass(frozen=True)
class TelemetryConfig:
    project_key: str
    host: str


def resolve_config() -> Optional[TelemetryConfig]:
    """`None` means telemetry stays off: either `JARVIS_TELEMETRY` is
    switched off, or there is no PostHog project key. The key is only
    baked into CI builds, so every source checkout is off by default and
    dev runs never reach the tester project."""
    if not telemetry_enabled():
        return None
    key = os.environ.get("JARVIS_POSTHOG_KEY") or _bundled_credentials.BUNDLED_POSTHOG_PROJECT_KEY
    if not key:
        return None
    host = os.environ.get("JARVIS_POSTHOG_HOST") or _bundled_credentials.BUNDLED_POSTHOG_HOST or DEFAULT_HOST
    return TelemetryConfig(project_key=key, host=host.rstrip("/"))
