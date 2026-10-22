"""MainWindow's updater wiring (jarvis.modules.desktop_app.main_window) -
one automatic check on construction, a manual check via the menu action,
and the full click -> download -> confirm -> install chain, each verified
by monkeypatching the updater modules' own entry points rather than
letting real network/subprocess calls happen. QApplication.quit() is
never called for real here - see MainWindow's own docstring for why it's
reached through self._quit_app instead of a bare module-level call.
"""

from pathlib import Path

import pytest
from PySide6.QtCore import QThreadPool

from jarvis_server.internal import env_config
from jarvis.modules.cloud import session_store
from jarvis.modules.desktop_app import main_window as main_window_mod
from jarvis.modules.desktop_app import update_checker, update_downloader, update_installer, version
from jarvis.modules.desktop_app.main_window import MainWindow


@pytest.fixture(autouse=True)
def _skip_first_run_settings(monkeypatch):
    monkeypatch.setattr(env_config, "has_embedding_api_key", lambda: True)
    monkeypatch.setattr(session_store, "load_session", lambda: ("user@example.com", "tok"))


@pytest.fixture(autouse=True)
def _dont_actually_spawn_a_daemon(monkeypatch):
    # These tests are about the updater, not daemon readiness - avoid a
    # real ensure_daemon_running() call trying to spawn a real subprocess
    # in the background during every test in this file.
    monkeypatch.setattr(main_window_mod, "ensure_daemon_running", lambda: 54321)


def _window(qtbot, monkeypatch, *, current_version="desktop-build-2026-01-01"):
    monkeypatch.setattr(version, "get_current_version", lambda: current_version)
    monkeypatch.setattr(main_window_mod.update_checker, "run_update_check", lambda *a, **k: None)
    window = MainWindow(QThreadPool())
    qtbot.addWidget(window)
    return window


def test_construction_triggers_exactly_one_automatic_update_check(qtbot, monkeypatch):
    calls = []
    monkeypatch.setattr(version, "get_current_version", lambda: "desktop-build-2026-01-01")
    monkeypatch.setattr(
        main_window_mod.update_checker, "run_update_check",
        lambda pool, current, on_result: calls.append((current, False)),
    )

    MainWindow(QThreadPool())

    assert calls == [("desktop-build-2026-01-01", False)]


def test_dev_build_skips_the_automatic_check_entirely(qtbot, monkeypatch):
    monkeypatch.setattr(version, "get_current_version", lambda: None)

    def _must_not_be_called(*a, **k):
        raise AssertionError("run_update_check should not be called for a dev build")

    monkeypatch.setattr(main_window_mod.update_checker, "run_update_check", _must_not_be_called)

    MainWindow(QThreadPool())  # must not raise


def test_check_for_updates_triggers_a_manual_check(qtbot, monkeypatch):
    # No menu bar action calls this anymore - the Settings page (a later
    # step) will wire its own "Check for Updates" button to this same
    # method, which MainWindow keeps unchanged (see main_window.py's
    # module docstring).
    window = _window(qtbot, monkeypatch)
    calls = []
    monkeypatch.setattr(
        main_window_mod.update_checker, "run_update_check",
        lambda pool, current, on_result: calls.append("manual"),
    )

    window._check_for_updates(manual=True)

    assert calls == ["manual"]


def test_manual_check_with_nothing_newer_shows_up_to_date(qtbot, monkeypatch):
    window = _window(qtbot, monkeypatch)
    seen = []
    monkeypatch.setattr(window.banner, "show_success", lambda msg: seen.append(msg))

    window._on_update_check_result(None, manual=True)

    assert seen and "up to date" in seen[0]


def test_automatic_check_with_nothing_newer_stays_silent(qtbot, monkeypatch):
    window = _window(qtbot, monkeypatch)

    def _must_not_be_called(msg):
        raise AssertionError("automatic check must stay silent when nothing is newer")

    monkeypatch.setattr(window.banner, "show_success", _must_not_be_called)
    monkeypatch.setattr(window.banner, "show_error", _must_not_be_called)

    window._on_update_check_result(None, manual=False)


def test_automatic_check_still_reports_to_settings_view(qtbot, monkeypatch):
    """Settings' own "checked N ago" label (Figma parity pass, Phase 10)
    needs every check's outcome, including the automatic startup one -
    the one check `_on_update_check_result` otherwise keeps silent on
    the main banner (see the test above)."""
    window = _window(qtbot, monkeypatch)
    calls = []
    monkeypatch.setattr(window.settings_view, "report_update_check_result", lambda found: calls.append(found))

    window._on_update_check_result(None, manual=False)

    assert calls == [False]


def test_update_found_shows_the_banner_and_stores_it_pending(qtbot, monkeypatch):
    window = _window(qtbot, monkeypatch)
    info = update_checker.UpdateInfo(tag="desktop-build-2026-02-01", download_url="https://x/y.dmg", html_url="")

    window._on_update_check_result(info, manual=False)

    assert window._pending_update == info
    assert not window.update_banner.isHidden()


def test_later_clears_the_pending_update(qtbot, monkeypatch):
    window = _window(qtbot, monkeypatch)
    info = update_checker.UpdateInfo(tag="desktop-build-2026-02-01", download_url="https://x/y.dmg", html_url="")
    window._on_update_check_result(info, manual=False)

    window.update_banner.later_clicked.emit()

    assert window._pending_update is None


def test_update_clicked_starts_the_download(qtbot, monkeypatch):
    window = _window(qtbot, monkeypatch)
    info = update_checker.UpdateInfo(tag="desktop-build-2026-02-01", download_url="https://x/y.dmg", html_url="")
    window._on_update_check_result(info, manual=False)

    calls = []
    monkeypatch.setattr(
        main_window_mod.update_downloader, "run_update_download",
        lambda pool, update_info, daemon_home, **kwargs: calls.append((update_info, kwargs)),
    )

    window.update_banner.update_clicked.emit()

    assert len(calls) == 1
    assert calls[0][0] == info
    assert "on_success" in calls[0][1] and "on_error" in calls[0][1]


def test_downloaded_shows_the_ready_banner_not_an_immediate_confirm(qtbot, monkeypatch):
    """DESIGN.md §6: a distinct "Update ready" state, not an immediate
    popup the instant the download finishes."""
    window = _window(qtbot, monkeypatch)

    window._on_update_downloaded(Path("/tmp/Jarvis-x.dmg"))

    assert window._downloaded_update_path == Path("/tmp/Jarvis-x.dmg")
    assert "ready" in window.update_banner.message().lower()
    assert window.update_banner._update_button.text() == "Restart now"


def test_restart_now_not_syncing_installs_without_confirming(qtbot, monkeypatch):
    window = _window(qtbot, monkeypatch)
    window._on_update_downloaded(Path("/tmp/Jarvis-x.dmg"))
    order = []
    monkeypatch.setattr(
        update_installer, "trigger_install_and_relaunch",
        lambda path, client_id: order.append(("install", path, client_id)),
    )
    monkeypatch.setattr(window, "_quit_app", lambda: order.append(("quit",)))

    window.update_banner.update_clicked.emit()

    assert order == [("install", Path("/tmp/Jarvis-x.dmg"), None), ("quit",)]


def test_restart_now_while_syncing_asks_to_confirm_first(qtbot, monkeypatch):
    from jarvis.modules.desktop_app import main_window as mod

    window = _window(qtbot, monkeypatch)
    window._on_update_downloaded(Path("/tmp/Jarvis-x.dmg"))
    window._syncing = True
    confirm_calls = []
    monkeypatch.setattr(mod, "confirm_action", lambda *a, **k: confirm_calls.append((a, k)) or True)
    order = []
    monkeypatch.setattr(
        update_installer, "trigger_install_and_relaunch",
        lambda path, client_id: order.append("install"),
    )
    monkeypatch.setattr(window, "_quit_app", lambda: order.append("quit"))

    window.update_banner.update_clicked.emit()

    assert confirm_calls[0][0][-1] == "Restart now"
    assert order == ["install", "quit"]


def test_declining_the_restart_confirmation_does_not_install_or_quit(qtbot, monkeypatch):
    from jarvis.modules.desktop_app import main_window as mod

    window = _window(qtbot, monkeypatch)
    window._on_update_downloaded(Path("/tmp/Jarvis-x.dmg"))
    window._syncing = True
    monkeypatch.setattr(mod, "confirm_action", lambda *a, **k: False)

    def _must_not_be_called(*a, **k):
        raise AssertionError("must not install/relaunch when the user declines")

    monkeypatch.setattr(update_installer, "trigger_install_and_relaunch", _must_not_be_called)
    monkeypatch.setattr(window, "_quit_app", _must_not_be_called)

    window.update_banner.update_clicked.emit()


def test_download_failure_shows_a_banner_not_a_modal(qtbot, monkeypatch):
    window = _window(qtbot, monkeypatch)

    window._on_update_download_failed(RuntimeError("network down"))

    assert "didn't download" in window.update_banner.message()
    assert window.update_banner._update_button.text() == "Retry"
    assert window.update_banner._later_button.isHidden()


def test_retrying_a_failed_download_re_downloads(qtbot, monkeypatch):
    window = _window(qtbot, monkeypatch)
    info = update_checker.UpdateInfo(tag="desktop-build-2026-02-01", download_url="https://x/y.dmg", html_url="")
    window._on_update_check_result(info, manual=False)
    window._on_update_download_failed(RuntimeError("network down"))

    calls = []
    monkeypatch.setattr(
        main_window_mod.update_downloader, "run_update_download",
        lambda pool, update_info, daemon_home, **kwargs: calls.append(update_info),
    )

    window.update_banner.update_clicked.emit()

    assert calls == [info]


def test_install_failure_offers_retry_and_download_manually(qtbot, monkeypatch):
    window = _window(qtbot, monkeypatch)
    window._on_update_downloaded(Path("/tmp/Jarvis-x.dmg"))

    def _boom(path, client_id):
        raise RuntimeError("Cannot install update: not running from a packaged .app.")

    monkeypatch.setattr(update_installer, "trigger_install_and_relaunch", _boom)

    window.update_banner.update_clicked.emit()

    assert "couldn't be installed" in window.update_banner.message()
    assert window.update_banner._update_button.text() == "Retry"
    assert window.update_banner._later_button.text() == "Download manually"


def test_download_manually_opens_the_releases_page(qtbot, monkeypatch):
    from jarvis.modules.desktop_app import main_window as mod

    window = _window(qtbot, monkeypatch)
    window._on_update_downloaded(Path("/tmp/Jarvis-x.dmg"))

    def _boom(path, client_id):
        raise RuntimeError("not packaged")

    monkeypatch.setattr(update_installer, "trigger_install_and_relaunch", _boom)
    window.update_banner.update_clicked.emit()  # -> install_failed mode

    opened = []
    monkeypatch.setattr(mod.QDesktopServices, "openUrl", staticmethod(lambda url: opened.append(url.toString())))

    window.update_banner.later_clicked.emit()

    assert opened == [update_checker.releases_page_url()]


def test_translocated_install_shows_a_blocked_message_with_no_retry(qtbot, monkeypatch):
    window = _window(qtbot, monkeypatch)
    window._on_update_downloaded(Path("/tmp/Jarvis-x.dmg"))

    def _blocked(path, client_id):
        raise update_installer.UpdateBlockedError(
            "Move Jarvis to your Applications folder and reopen it to update."
        )

    monkeypatch.setattr(update_installer, "trigger_install_and_relaunch", _blocked)

    window.update_banner.update_clicked.emit()

    assert window.update_banner.message() == "Move Jarvis to your Applications folder and reopen it to update."
    assert window.update_banner._update_button.isHidden()
    assert window.update_banner._later_button.text() == "Dismiss"


def test_available_button_reads_update_not_update_and_restart(qtbot, monkeypatch):
    window = _window(qtbot, monkeypatch)
    info = update_checker.UpdateInfo(tag="desktop-build-2026-02-01", download_url="https://x/y.dmg", html_url="")

    window._on_update_check_result(info, manual=False)

    assert window.update_banner._update_button.text() == "Update"


def test_downloading_progress_shows_megabytes_once_total_is_known(qtbot, monkeypatch):
    window = _window(qtbot, monkeypatch)
    info = update_checker.UpdateInfo(tag="desktop-build-2026-02-01", download_url="https://x/y.dmg", html_url="")
    window._on_update_check_result(info, manual=False)
    window.update_banner.show_downloading()

    window.update_banner.set_progress(2_300_000, 48_000_000)

    assert window.update_banner.message() == "Downloading the update. 2.3 of 48.0 MB. You can keep working."
