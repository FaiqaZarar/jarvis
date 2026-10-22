import pytest


def pytest_addoption(parser):
    parser.addoption(
        "--run-live", action="store_true", default=False,
        help="Actually run live-API tests (they cost money and need network).",
    )
    parser.addoption(
        "--run-slow", action="store_true", default=False,
        help="Actually run slow_cross_process tests (real subprocess spawns, ~30s+ each).",
    )


@pytest.fixture(autouse=True)
def _cloud_server_configured(monkeypatch):
    """Tests run as an official build does, with a cloud server for
    sign-in and feedback. A test of a build from source, which has none,
    sets `cloud_client._BASE_URL` to "" itself."""
    from jarvis.modules.cloud import client as cloud_client

    monkeypatch.setattr(cloud_client, "_BASE_URL", "https://cloud.test")
