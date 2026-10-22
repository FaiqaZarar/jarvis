"""jarvis.modules.cloud.session_store - keyring-backed session storage.
Monkeypatches keyring's module-level functions directly rather than
switching backends, so this runs identically on a CI box with no real
OS credential store configured."""

import pytest

from jarvis.modules.cloud import session_store


@pytest.fixture
def fake_keyring(monkeypatch):
    store: dict[tuple[str, str], str] = {}

    def _set_password(service, key, value):
        store[(service, key)] = value

    def _get_password(service, key):
        return store.get((service, key))

    def _delete_password(service, key):
        if (service, key) not in store:
            raise session_store.keyring.errors.PasswordDeleteError()
        del store[(service, key)]

    monkeypatch.setattr(session_store.keyring, "set_password", _set_password)
    monkeypatch.setattr(session_store.keyring, "get_password", _get_password)
    monkeypatch.setattr(session_store.keyring, "delete_password", _delete_password)
    return store


def test_load_session_is_none_when_nothing_saved(fake_keyring):
    assert session_store.load_session() is None


def test_save_then_load_round_trips(fake_keyring):
    session_store.save_session("user@example.com", "tok-123")
    assert session_store.load_session() == ("user@example.com", "tok-123")


def test_clear_session_removes_it(fake_keyring):
    session_store.save_session("user@example.com", "tok-123")
    session_store.clear_session()
    assert session_store.load_session() is None


def test_clear_session_is_safe_when_nothing_was_saved(fake_keyring):
    session_store.clear_session()  # must not raise
    assert session_store.load_session() is None
