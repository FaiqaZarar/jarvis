"""jarvis.modules.desktop_app.update_checker - check_for_update() never
raises (see its own docstring), and the threading wrapper follows the
same shape/lifetime pattern as daemon_worker.py's, verified the same way
test_desktop_app_daemon_worker.py verifies that one.

Pinned to "darwin" by an autouse fixture below so every pre-existing
.dmg-based test here behaves identically regardless of which platform
actually runs this suite - the platform-dispatch logic itself
(_asset_suffix_for_platform, and matching a .exe asset on win32) gets
its own dedicated tests further down, which override the pin."""

import threading

import pytest
from PySide6.QtCore import QThreadPool

from jarvis.modules.desktop_app import update_checker
from jarvis.modules.desktop_app.update_checker import UpdateInfo, check_for_update


@pytest.fixture(autouse=True)
def _pin_platform_to_darwin(monkeypatch):
    monkeypatch.setattr(update_checker.sys, "platform", "darwin")


class _FakeResponse:
    def __init__(self, *, status_code=200, json_body=None, raise_for_status_exc=None):
        self.status_code = status_code
        self._json_body = json_body
        self._raise_for_status_exc = raise_for_status_exc

    def raise_for_status(self):
        if self._raise_for_status_exc is not None:
            raise self._raise_for_status_exc

    def json(self):
        return self._json_body


def _release_body(tag, *, dmg_url="https://example.com/Jarvis.dmg", assets=None):
    return {
        "tag_name": tag,
        "html_url": f"https://example.com/releases/{tag}",
        "assets": assets if assets is not None else [
            {"name": "Jarvis.dmg", "browser_download_url": dmg_url},
        ],
    }


def test_returns_none_without_calling_requests_when_current_version_is_none(monkeypatch):
    def _must_not_be_called(*a, **k):
        raise AssertionError("requests.get should not be called when current_version is None")

    monkeypatch.setattr(update_checker.requests, "get", _must_not_be_called)

    assert check_for_update(None) is None


def test_finds_a_newer_release(monkeypatch):
    monkeypatch.setattr(
        update_checker.requests, "get",
        lambda *a, **k: _FakeResponse(json_body=_release_body("desktop-build-2026-02-01")),
    )

    result = check_for_update("desktop-build-2026-01-01")

    assert result == UpdateInfo(
        tag="desktop-build-2026-02-01",
        download_url="https://example.com/Jarvis.dmg",
        html_url="https://example.com/releases/desktop-build-2026-02-01",
        asset_name="Jarvis.dmg",
    )


def test_returns_none_when_latest_tag_equals_current(monkeypatch):
    monkeypatch.setattr(
        update_checker.requests, "get",
        lambda *a, **k: _FakeResponse(json_body=_release_body("desktop-build-2026-01-01")),
    )

    assert check_for_update("desktop-build-2026-01-01") is None


def test_returns_none_when_latest_tag_is_older(monkeypatch):
    monkeypatch.setattr(
        update_checker.requests, "get",
        lambda *a, **k: _FakeResponse(json_body=_release_body("desktop-build-2025-12-01")),
    )

    assert check_for_update("desktop-build-2026-01-01") is None


def test_returns_none_when_release_has_no_dmg_asset(monkeypatch):
    monkeypatch.setattr(
        update_checker.requests, "get",
        lambda *a, **k: _FakeResponse(json_body=_release_body("desktop-build-2026-02-01", assets=[])),
    )

    assert check_for_update("desktop-build-2026-01-01") is None


def test_returns_none_on_malformed_json(monkeypatch):
    class _BadResponse(_FakeResponse):
        def json(self):
            raise ValueError("not json")

    monkeypatch.setattr(update_checker.requests, "get", lambda *a, **k: _BadResponse())

    assert check_for_update("desktop-build-2026-01-01") is None


def test_returns_none_on_a_raised_request_exception(monkeypatch):
    import requests

    def _boom(*a, **k):
        raise requests.ConnectionError("no network")

    monkeypatch.setattr(update_checker.requests, "get", _boom)

    assert check_for_update("desktop-build-2026-01-01") is None


def test_returns_none_on_a_non_2xx_status(monkeypatch):
    import requests

    def _fake_get(*a, **k):
        return _FakeResponse(status_code=404, raise_for_status_exc=requests.HTTPError("404"))

    monkeypatch.setattr(update_checker.requests, "get", _fake_get)

    assert check_for_update("desktop-build-2026-01-01") is None


# ---------------------------------------------------------------------
# Per-platform asset matching - _asset_suffix_for_platform and the
# check_for_update() branch that uses it. Each test sets sys.platform
# itself, overriding the module's own "darwin" pin above.
# ---------------------------------------------------------------------

def test_asset_suffix_for_platform_darwin_is_dmg(monkeypatch):
    monkeypatch.setattr(update_checker.sys, "platform", "darwin")
    assert update_checker._asset_suffix_for_platform() == ".dmg"


def test_asset_suffix_for_platform_win32_is_exe(monkeypatch):
    monkeypatch.setattr(update_checker.sys, "platform", "win32")
    assert update_checker._asset_suffix_for_platform() == ".exe"


def test_asset_suffix_for_platform_unsupported_is_none(monkeypatch):
    monkeypatch.setattr(update_checker.sys, "platform", "linux")
    assert update_checker._asset_suffix_for_platform() is None


def test_returns_none_on_an_unsupported_platform_without_calling_requests(monkeypatch):
    monkeypatch.setattr(update_checker.sys, "platform", "linux")

    def _must_not_be_called(*a, **k):
        raise AssertionError("requests.get should not be called on a platform with no matching asset")

    monkeypatch.setattr(update_checker.requests, "get", _must_not_be_called)

    assert check_for_update("desktop-build-2026-01-01") is None


def test_finds_the_windows_asset_on_win32_and_ignores_the_mac_one(monkeypatch):
    monkeypatch.setattr(update_checker.sys, "platform", "win32")
    monkeypatch.setattr(
        update_checker.requests, "get",
        lambda *a, **k: _FakeResponse(json_body=_release_body(
            "desktop-build-2026-02-01",
            assets=[
                {"name": "Jarvis.dmg", "browser_download_url": "https://example.com/Jarvis.dmg"},
                {"name": "JarvisSetup.exe", "browser_download_url": "https://example.com/JarvisSetup.exe"},
            ],
        )),
    )

    result = check_for_update("desktop-build-2026-01-01")

    assert result == UpdateInfo(
        tag="desktop-build-2026-02-01",
        download_url="https://example.com/JarvisSetup.exe",
        html_url="https://example.com/releases/desktop-build-2026-02-01",
        asset_name="JarvisSetup.exe",
    )


def test_returns_none_on_win32_when_release_has_only_a_dmg_asset(monkeypatch):
    monkeypatch.setattr(update_checker.sys, "platform", "win32")
    monkeypatch.setattr(
        update_checker.requests, "get",
        lambda *a, **k: _FakeResponse(json_body=_release_body("desktop-build-2026-02-01")),
    )

    assert check_for_update("desktop-build-2026-01-01") is None


# ---------------------------------------------------------------------
# Threading wrapper - same shape/lifetime pattern as daemon_worker.py's,
# see tests/test_desktop_app_daemon_worker.py for the original.
# ---------------------------------------------------------------------

def test_worker_runs_on_a_different_thread_than_the_caller(qtbot, monkeypatch):
    calling_thread_id = threading.get_ident()
    seen = {}

    def _fake_get(*a, **k):
        seen["thread_id"] = threading.get_ident()
        return _FakeResponse(json_body=_release_body("desktop-build-2026-02-01"))

    monkeypatch.setattr(update_checker.requests, "get", _fake_get)

    pool = QThreadPool()
    received = []

    update_checker.run_update_check(pool, "desktop-build-2026-01-01", on_result=received.append)

    qtbot.waitUntil(lambda: len(received) == 1, timeout=2000)
    assert received[0].tag == "desktop-build-2026-02-01"
    assert seen["thread_id"] != calling_thread_id


def test_run_update_check_delivers_none_when_nothing_newer(qtbot):
    pool = QThreadPool()
    received = []

    update_checker.run_update_check(pool, None, on_result=received.append)

    qtbot.waitUntil(lambda: received == [None], timeout=2000)
