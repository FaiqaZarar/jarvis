"""jarvis.modules.desktop_app.version - see that module's own docstring
for why None is the safe fallback for every case except "a frozen build
with a real VERSION file next to it"."""

import sys

import pytest

from jarvis.modules.desktop_app import version


@pytest.fixture(autouse=True)
def _no_real_frozen_state(monkeypatch):
    monkeypatch.delattr(sys, "frozen", raising=False)
    monkeypatch.delattr(sys, "_MEIPASS", raising=False)


def test_returns_none_when_not_frozen(monkeypatch, tmp_path):
    (tmp_path / "VERSION").write_text("desktop-build-2026-01-01")
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)

    assert version.get_current_version() is None


def test_returns_the_stamped_tag_when_frozen(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
    (tmp_path / "VERSION").write_text("desktop-build-2026-01-01\n")

    assert version.get_current_version() == "desktop-build-2026-01-01"


def test_returns_none_when_frozen_but_version_file_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)

    assert version.get_current_version() is None


def test_returns_none_when_frozen_but_meipass_missing(monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)

    assert version.get_current_version() is None


def test_returns_none_when_version_file_is_blank(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
    (tmp_path / "VERSION").write_text("   \n")

    assert version.get_current_version() is None
