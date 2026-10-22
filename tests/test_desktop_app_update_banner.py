"""jarvis.modules.desktop_app.update_banner - visibility/label state and
signal-on-click wiring. No fade-timing assertions (see
error_presentation.py's own tests, if any, for that precedent) - the
opacity animation is cosmetic, not behavior worth asserting on."""

from jarvis.modules.desktop_app.update_banner import UpdateBanner


def test_show_update_available_sets_label_and_shows_buttons(qtbot):
    banner = UpdateBanner()
    qtbot.addWidget(banner)

    banner.show_update_available("desktop-build-2026-02-01")

    assert "desktop-build-2026-02-01" in banner.message()
    assert banner._update_button.isVisible()
    assert banner._later_button.isVisible()
    assert not banner._progress.isVisible()
    assert not banner.isHidden()


def test_update_button_click_emits_update_clicked(qtbot):
    banner = UpdateBanner()
    qtbot.addWidget(banner)
    banner.show_update_available("desktop-build-2026-02-01")

    with qtbot.waitSignal(banner.update_clicked, timeout=1000):
        banner._update_button.click()


def test_later_button_click_emits_later_clicked_and_dismisses(qtbot):
    banner = UpdateBanner()
    qtbot.addWidget(banner)
    banner.show_update_available("desktop-build-2026-02-01")

    with qtbot.waitSignal(banner.later_clicked, timeout=1000):
        banner._later_button.click()

    qtbot.waitUntil(banner.isHidden, timeout=1000)


def test_show_downloading_hides_buttons_and_shows_progress(qtbot):
    banner = UpdateBanner()
    qtbot.addWidget(banner)

    banner.show_downloading()

    assert not banner._update_button.isVisible()
    assert not banner._later_button.isVisible()
    assert banner._progress.isVisible()


def test_set_progress_with_a_known_total(qtbot):
    banner = UpdateBanner()
    qtbot.addWidget(banner)

    banner.set_progress(50, 100)

    assert banner._progress.minimum() == 0
    assert banner._progress.maximum() == 100
    assert banner._progress.value() == 50


def test_set_progress_with_no_total_is_indeterminate(qtbot):
    banner = UpdateBanner()
    qtbot.addWidget(banner)

    banner.set_progress(50, None)

    assert banner._progress.minimum() == 0
    assert banner._progress.maximum() == 0


def test_show_ready_relabels_the_update_button_to_restart_now(qtbot):
    banner = UpdateBanner()
    qtbot.addWidget(banner)

    banner.show_ready()

    assert "ready" in banner.message().lower()
    assert banner._update_button.text() == "Restart now"
    assert banner._later_button.isVisible()
    assert not banner.isHidden()


def test_show_installing_hides_both_buttons(qtbot):
    banner = UpdateBanner()
    qtbot.addWidget(banner)

    banner.show_installing()

    assert "Installing" in banner.message()
    assert not banner._update_button.isVisible()
    assert not banner._later_button.isVisible()


def test_show_download_failed_offers_only_retry(qtbot):
    banner = UpdateBanner()
    qtbot.addWidget(banner)

    banner.show_download_failed()

    assert "didn't download" in banner.message()
    assert banner._update_button.text() == "Retry"
    assert banner._update_button.isVisible()
    assert not banner._later_button.isVisible()


def test_show_install_failed_offers_retry_and_download_manually(qtbot):
    banner = UpdateBanner()
    qtbot.addWidget(banner)

    banner.show_install_failed()

    assert "couldn't be installed" in banner.message()
    assert banner._update_button.text() == "Retry"
    assert banner._later_button.text() == "Download manually"
    assert banner._later_button.isVisible()


def test_show_blocked_hides_the_update_button_and_offers_only_dismiss(qtbot):
    banner = UpdateBanner()
    qtbot.addWidget(banner)

    banner.show_blocked("Move Jarvis to your Applications folder and reopen it to update.")

    assert banner.message() == "Move Jarvis to your Applications folder and reopen it to update."
    assert not banner._update_button.isVisible()
    assert banner._later_button.text() == "Dismiss"
