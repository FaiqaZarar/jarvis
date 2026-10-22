"""A build from source has no cloud server (cloud_client.is_configured()
is False): the app must work fully without one, with sign-in, feedback
and error reporting simply absent rather than broken."""

import pytest
from PySide6.QtCore import QThreadPool

from jarvis.modules.cloud import client as cloud_client
from jarvis.modules.cloud import session_store
from jarvis.modules.desktop_app import error_reporting
from jarvis.modules.desktop_app.error_presentation import DaemonStatusBanner
from jarvis.modules.desktop_app.views.connectors_view import ConnectorsView
from jarvis.modules.desktop_app.views.settings_view import SettingsView


@pytest.fixture(autouse=True)
def _no_cloud_server(monkeypatch):
    monkeypatch.setattr(cloud_client, "_BASE_URL", "")
    monkeypatch.setattr(session_store, "load_session", lambda: None)


def test_no_server_means_not_configured():
    assert cloud_client.is_configured() is False


def test_the_main_window_does_not_ask_to_sign_in(qtbot, monkeypatch):
    from jarvis.modules.desktop_app import main_window as main_window_mod

    monkeypatch.setattr(main_window_mod, "run_daemon_call", lambda *a, **k: None)
    sign_in_prompts = []
    monkeypatch.setattr(
        main_window_mod.MainWindow, "_show_first_run_sign_in", lambda self: sign_in_prompts.append(True) or False,
    )

    window = main_window_mod.MainWindow(QThreadPool())
    qtbot.addWidget(window)

    assert sign_in_prompts == []


def test_the_setup_checklist_hides_the_sign_in_step_and_counts_it_done(qtbot, monkeypatch):
    from jarvis.modules.desktop_app import claude_desktop_setup as cds
    from jarvis.modules.desktop_app.views import connectors_view as connectors_view_mod

    plan = cds.ConnectClaudeDesktopPlan(config_path=None, case=cds.CASE_MISSING, instructions="", snippet=None)
    monkeypatch.setattr(connectors_view_mod.cds, "build_plan", lambda: plan)
    built = []
    real_build = ConnectorsView._build_checklist_items
    monkeypatch.setattr(
        ConnectorsView, "_build_checklist_items",
        lambda self, health: built.append(health) or real_build(self, health),
    )
    view = ConnectorsView(QThreadPool())
    qtbot.addWidget(view)

    view.refresh_setup_checklist()

    health = built[-1]
    assert health.signed_in is True
    sign_in_item = view._build_checklist_items(health)[0]
    assert sign_in_item.label == "Signed in"
    assert sign_in_item.state == "dismissed"


def test_settings_has_no_account_or_feedback_section(qtbot):
    from PySide6.QtWidgets import QGroupBox

    view = SettingsView(QThreadPool(), DaemonStatusBanner(), lambda: None)
    qtbot.addWidget(view)
    view.refresh_account_label()  # MainWindow calls this after the service is ready

    titles = {group.title() for group in view.findChildren(QGroupBox)}
    assert "Account" not in titles
    assert "Feedback" not in titles


def test_an_unknown_sync_failure_offers_no_feedback_button(qtbot):
    view = ConnectorsView(QThreadPool())
    qtbot.addWidget(view)

    view._show_sync_failure("unknown", "Something went wrong. Everything already saved is safe.")

    assert view._status_banner._secondary_button.isHidden()


def test_error_reports_are_never_sent(monkeypatch):
    sent = []
    monkeypatch.setattr(error_reporting, "run_daemon_call", lambda *a, **k: sent.append(a))
    monkeypatch.setattr(session_store, "load_session", lambda: ("someone@example.com", "tok"))

    error_reporting.report_error(QThreadPool(), "drive_connect_failed", "boom")

    assert sent == []
