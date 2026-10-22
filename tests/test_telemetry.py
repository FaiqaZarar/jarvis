"""jarvis_server.internal.telemetry - setup only. What gets sent (and
the privacy filter) is covered separately once it exists."""

import pytest

from jarvis_server.internal import _bundled_credentials, telemetry
from jarvis_server.internal.telemetry import config as config_mod
from jarvis_server.internal.telemetry.install_id import get_install_id


@pytest.fixture(autouse=True)
def _no_key(monkeypatch):
    for var in ("JARVIS_POSTHOG_KEY", "JARVIS_POSTHOG_HOST", "JARVIS_TELEMETRY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(_bundled_credentials, "BUNDLED_POSTHOG_PROJECT_KEY", "")
    monkeypatch.setattr(_bundled_credentials, "BUNDLED_POSTHOG_HOST", "")
    monkeypatch.setattr(telemetry, "_session", None)


def test_off_without_a_key():
    assert config_mod.resolve_config() is None


def test_env_key_defaults_to_eu_host(monkeypatch):
    monkeypatch.setenv("JARVIS_POSTHOG_KEY", "phc_test")
    cfg = config_mod.resolve_config()
    assert cfg.project_key == "phc_test"
    assert cfg.host == "https://eu.i.posthog.com"


def test_bundled_key_and_host_used_when_env_unset(monkeypatch):
    monkeypatch.setattr(_bundled_credentials, "BUNDLED_POSTHOG_PROJECT_KEY", "phc_bundled")
    monkeypatch.setattr(_bundled_credentials, "BUNDLED_POSTHOG_HOST", "https://us.i.posthog.com/")
    cfg = config_mod.resolve_config()
    assert cfg.project_key == "phc_bundled"
    assert cfg.host == "https://us.i.posthog.com"


def test_env_key_wins_over_bundled(monkeypatch):
    monkeypatch.setattr(_bundled_credentials, "BUNDLED_POSTHOG_PROJECT_KEY", "phc_bundled")
    monkeypatch.setenv("JARVIS_POSTHOG_KEY", "phc_env")
    assert config_mod.resolve_config().project_key == "phc_env"


@pytest.mark.parametrize("value", ["0", "false", "OFF", "no"])
def test_switch_off_overrides_a_key(monkeypatch, value):
    monkeypatch.setenv("JARVIS_POSTHOG_KEY", "phc_test")
    monkeypatch.setenv("JARVIS_TELEMETRY", value)
    assert config_mod.resolve_config() is None


def test_install_id_is_stable_across_calls(tmp_path):
    first = get_install_id(tmp_path)
    assert first
    assert get_install_id(tmp_path) == first


def test_install_id_falls_back_when_home_is_unwritable(tmp_path):
    not_a_dir = tmp_path / "file"
    not_a_dir.write_text("x")
    assert get_install_id(not_a_dir)


def test_init_without_key_stays_off(tmp_path):
    assert telemetry.init("jarvis-test", tmp_path) is False
    assert telemetry.is_enabled() is False
    telemetry.shutdown()


class _FakePosthog:
    instances = []

    def __init__(self, key, **kwargs):
        self.key, self.kwargs, self.shut_down = key, kwargs, False
        _FakePosthog.instances.append(self)

    def shutdown(self):
        self.shut_down = True


def test_init_with_key_starts_both_and_shutdown_stops_them(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_POSTHOG_KEY", "phc_test")
    monkeypatch.setattr(telemetry, "Posthog", _FakePosthog)
    installed = []
    monkeypatch.setattr(telemetry.trace, "set_tracer_provider", installed.append)

    assert telemetry.init("jarvis-test", tmp_path) is True
    assert telemetry.is_enabled()
    client = _FakePosthog.instances[-1]
    assert client.key == "phc_test"
    assert client.kwargs["disable_geoip"] is True
    assert client.kwargs["capture_exception_code_variables"] is False
    resource = installed[0].resource.attributes
    assert resource["service.name"] == "jarvis-test"
    assert resource["jarvis.install_id"] == get_install_id(tmp_path)

    assert telemetry.init("jarvis-test", tmp_path) is True
    assert len(installed) == 1

    telemetry.shutdown()
    assert client.shut_down
    assert not telemetry.is_enabled()


def test_shutdown_gives_up_instead_of_blocking_quit(monkeypatch, tmp_path):
    import threading
    import time

    monkeypatch.setenv("JARVIS_POSTHOG_KEY", "phc_test")
    monkeypatch.setattr(telemetry, "Posthog", _FakePosthog)
    monkeypatch.setattr(telemetry.trace, "set_tracer_provider", lambda p: None)
    monkeypatch.setattr(telemetry, "_SHUTDOWN_BUDGET_S", 0.2)
    telemetry.init("jarvis-test", tmp_path)
    release = threading.Event()
    _FakePosthog.instances[-1].shutdown = lambda: release.wait(5)

    start = time.monotonic()
    telemetry.shutdown()
    assert time.monotonic() - start < 1.5
    release.set()


def test_init_failure_leaves_telemetry_off(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_POSTHOG_KEY", "phc_test")

    def boom(*args, **kwargs):
        raise RuntimeError("no network")

    monkeypatch.setattr(telemetry, "Posthog", boom)
    assert telemetry.init("jarvis-test", tmp_path) is False
    assert not telemetry.is_enabled()


def test_tracer_is_usable_while_off():
    with telemetry.get_tracer("t").start_as_current_span("noop"):
        pass
