"""jarvis_server.internal.env_config: dev-mode `.env` (unchanged) vs. the
packaged app's own settings file at `<daemon_home>/.env`
(docs/desktop-app-DESIGN.md §6/§8.6's `.env`-discovery gap, §8.8's
first-run settings screen).

Every test here fakes the bare, cwd-based `load_dotenv()` call so it
never touches this repo's own real `.env` (which has a real key) -
"dev mode already found something" is simulated directly via
`monkeypatch.setenv` instead, exactly matching what a real, successful
dev-mode load would have left behind in `os.environ`.
"""

import os
import sys

import pytest
from dotenv import load_dotenv as _real_load_dotenv

from jarvis_server.internal import _bundled_credentials, env_config

_MANAGED_KEYS = (
    "EMBEDDING_API_KEY", "EMBEDDING_MODEL", "EMBEDDING_API_BASE",
    "GOOGLE_OAUTH_CLIENT_ID", "GOOGLE_OAUTH_CLIENT_SECRET",
)


@pytest.fixture(autouse=True)
def _isolated_dotenv(monkeypatch):
    def _fake_load_dotenv(dotenv_path=None, *args, **kwargs):
        if dotenv_path is None:
            return False  # never let the real upward search touch this repo's real .env
        return _real_load_dotenv(dotenv_path, *args, **kwargs)

    monkeypatch.setattr(env_config, "load_dotenv", _fake_load_dotenv)
    # The real BUNDLED_GOOGLE_OAUTH_CLIENT_ID/_SECRET (_bundled_credentials.py,
    # CI-overwritten at build time - docs/desktop-app-drive-connector-
    # DESIGN.md §4) are the project's actual shared credentials - every
    # test below fakes them to obviously-not-real placeholder values
    # instead, so a real secret never flows through a test run (a failed
    # assertion's diff could otherwise print it) and no test here is
    # coupled to what the real values happen to be.
    monkeypatch.setattr(_bundled_credentials, "BUNDLED_GOOGLE_OAUTH_CLIENT_ID", "test-bundled-client-id")
    monkeypatch.setattr(_bundled_credentials, "BUNDLED_GOOGLE_OAUTH_CLIENT_SECRET", "test-bundled-client-secret")
    monkeypatch.setattr(_bundled_credentials, "BUNDLED_EMBEDDING_API_KEY", "test-bundled-embedding-key")
    for key in _MANAGED_KEYS:
        monkeypatch.delenv(key, raising=False)
    yield
    # save_settings()/load_env() write directly to os.environ (by
    # design, so the change applies immediately) - not through
    # monkeypatch, so it isn't auto-reverted. Clean up explicitly.
    for key in _MANAGED_KEYS:
        os.environ.pop(key, None)


# ---------------------------------------------------------------------
# has_embedding_api_key() - the first-run detection check
# ---------------------------------------------------------------------

def test_has_embedding_api_key_false_when_unset():
    assert env_config.has_embedding_api_key() is False


def test_has_embedding_api_key_true_when_set(monkeypatch):
    monkeypatch.setenv("EMBEDDING_API_KEY", "sk-real")
    assert env_config.has_embedding_api_key() is True


# ---------------------------------------------------------------------
# load_env(): dev-mode first, packaged fallback second, never overriding
# ---------------------------------------------------------------------

def test_load_env_picks_up_the_packaged_fallback_when_dev_mode_has_nothing(tmp_path):
    (tmp_path / ".env").write_text("EMBEDDING_API_KEY=packaged-key\nEMBEDDING_MODEL=packaged-model\n")

    env_config.load_env(tmp_path)

    assert os.environ["EMBEDDING_API_KEY"] == "packaged-key"
    assert os.environ["EMBEDDING_MODEL"] == "packaged-model"


def test_load_env_dev_mode_value_wins_over_the_packaged_fallback(tmp_path, monkeypatch):
    # Simulates dev-mode's own load_dotenv() having already found a real
    # repo-relative .env before the packaged fallback is even considered.
    monkeypatch.setenv("EMBEDDING_API_KEY", "dev-key")
    (tmp_path / ".env").write_text("EMBEDDING_API_KEY=packaged-key\n")

    env_config.load_env(tmp_path)

    assert os.environ["EMBEDDING_API_KEY"] == "dev-key"  # unchanged - dev mode wins


def test_load_env_with_nothing_configured_is_a_safe_no_op(monkeypatch):
    monkeypatch.delenv("JARVIS_DAEMON_HOME", raising=False)

    env_config.load_env()  # must not raise even with nowhere to look

    # The bundled embedding key fallback doesn't depend on JARVIS_DAEMON_HOME
    # existing at all - it's a plain os.environ default, so it still
    # applies here (see test_has_embedding_api_key_true_from_the_bundled_
    # fallback_alone for the behavior this enables).
    assert os.environ["EMBEDDING_API_KEY"] == "test-bundled-embedding-key"


def test_load_env_falls_back_to_the_JARVIS_DAEMON_HOME_env_var(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_DAEMON_HOME", str(tmp_path))
    (tmp_path / ".env").write_text("EMBEDDING_API_KEY=from-env-var-home\n")

    env_config.load_env()  # no explicit daemon_home - must fall back to the env var

    assert os.environ["EMBEDDING_API_KEY"] == "from-env-var-home"


# ---------------------------------------------------------------------
# The bundled Google Drive OAuth client
# (docs/desktop-app-drive-connector-DESIGN.md §4) - the fixture above
# fakes BUNDLED_GOOGLE_OAUTH_CLIENT_ID/_SECRET to placeholder values
# for every test in this file, so none of these are coupled to (or ever
# touch) the real, shared credentials.
# ---------------------------------------------------------------------

def test_load_env_falls_back_to_the_bundled_google_oauth_client_when_unset(tmp_path):
    env_config.load_env(tmp_path)  # nothing in dev-mode or the packaged .env sets these

    assert os.environ["GOOGLE_OAUTH_CLIENT_ID"] == "test-bundled-client-id"
    assert os.environ["GOOGLE_OAUTH_CLIENT_SECRET"] == "test-bundled-client-secret"


def test_load_env_a_real_dot_env_value_wins_over_the_bundled_google_oauth_client(tmp_path):
    (tmp_path / ".env").write_text("GOOGLE_OAUTH_CLIENT_ID=real-client-id\n")

    env_config.load_env(tmp_path)

    assert os.environ["GOOGLE_OAUTH_CLIENT_ID"] == "real-client-id"
    # The secret wasn't in that .env - still falls back to the bundled one independently.
    assert os.environ["GOOGLE_OAUTH_CLIENT_SECRET"] == "test-bundled-client-secret"


def test_load_env_dev_mode_google_oauth_value_also_wins_over_the_bundled_client(tmp_path, monkeypatch):
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "dev-client-id")

    env_config.load_env(tmp_path)

    assert os.environ["GOOGLE_OAUTH_CLIENT_ID"] == "dev-client-id"  # unchanged - dev mode wins


def test_apply_bundled_google_oauth_client_is_a_no_op_when_the_bundled_values_are_blank(monkeypatch):
    """The real, pre-fill state (env_config.py's own former placeholder)
    - confirms a blank bundled value is genuinely inert, not just
    "usually harmless," for anyone building without the real ones set
    (e.g. a CI job, or a contributor without access to them)."""
    monkeypatch.setattr(_bundled_credentials, "BUNDLED_GOOGLE_OAUTH_CLIENT_ID", "")
    monkeypatch.setattr(_bundled_credentials, "BUNDLED_GOOGLE_OAUTH_CLIENT_SECRET", "")

    env_config._apply_bundled_google_oauth_client()

    assert "GOOGLE_OAUTH_CLIENT_ID" not in os.environ
    assert "GOOGLE_OAUTH_CLIENT_SECRET" not in os.environ


# ---------------------------------------------------------------------
# The bundled embedding API key (test-phase-only convenience - see
# env_config.py's own module docstring) - same fallback-only precedence
# as the Google OAuth client above.
# ---------------------------------------------------------------------

def test_load_env_falls_back_to_the_bundled_embedding_key_when_unset(tmp_path):
    env_config.load_env(tmp_path)  # nothing in dev-mode or the packaged .env sets this

    assert os.environ["EMBEDDING_API_KEY"] == "test-bundled-embedding-key"


def test_load_env_a_real_dot_env_value_wins_over_the_bundled_embedding_key(tmp_path):
    (tmp_path / ".env").write_text("EMBEDDING_API_KEY=real-key\n")

    env_config.load_env(tmp_path)

    assert os.environ["EMBEDDING_API_KEY"] == "real-key"  # unchanged - the packaged .env wins


def test_load_env_dev_mode_wins_over_the_bundled_embedding_key(monkeypatch, tmp_path):
    monkeypatch.setenv("EMBEDDING_API_KEY", "dev-key")  # simulates a real dev-mode load_dotenv() having already run

    env_config.load_env(tmp_path)

    assert os.environ["EMBEDDING_API_KEY"] == "dev-key"  # unchanged - dev mode wins


def test_apply_bundled_embedding_key_is_a_no_op_when_the_bundled_value_is_blank(monkeypatch):
    monkeypatch.setattr(_bundled_credentials, "BUNDLED_EMBEDDING_API_KEY", "")

    env_config._apply_bundled_embedding_key()

    assert "EMBEDDING_API_KEY" not in os.environ


def test_has_embedding_api_key_true_from_the_bundled_fallback_alone(tmp_path):
    """The actual point of bundling this: has_embedding_api_key() - the
    exact check MainWindow's first-run gate uses - is satisfied by the
    bundled value alone, with no .env and no dev-mode value anywhere, so
    a tester never sees the first-run settings dialog at all."""
    env_config.load_env(tmp_path)

    assert env_config.has_embedding_api_key() is True


# ---------------------------------------------------------------------
# save_settings() / read_saved_settings(): the settings dialog's actual
# persistence, to the correct OS-standard per-user location.
# ---------------------------------------------------------------------

def test_save_settings_writes_to_daemon_home_dot_env(tmp_path):
    written_path = env_config.save_settings("sk-new", "voyageai/voyage-4", "", daemon_home=tmp_path)

    assert written_path == tmp_path / ".env"
    assert written_path.exists()
    assert "sk-new" in written_path.read_text()


def test_save_settings_creates_the_daemon_home_directory_if_missing(tmp_path):
    fresh_home = tmp_path / "not-yet-created"

    env_config.save_settings("sk-new", "voyageai/voyage-4", daemon_home=fresh_home)

    assert (fresh_home / ".env").exists()


def test_save_settings_applies_immediately_to_this_process(tmp_path):
    env_config.save_settings("sk-immediate", "a-model", "https://example.test", daemon_home=tmp_path)

    assert os.environ["EMBEDDING_API_KEY"] == "sk-immediate"
    assert os.environ["EMBEDDING_MODEL"] == "a-model"
    assert os.environ["EMBEDDING_API_BASE"] == "https://example.test"


def test_save_settings_omits_api_base_when_left_blank(tmp_path):
    written_path = env_config.save_settings("sk-new", "voyageai/voyage-4", "", daemon_home=tmp_path)

    assert "EMBEDDING_API_BASE" not in written_path.read_text()
    assert "EMBEDDING_API_BASE" not in os.environ


def test_save_settings_raises_without_a_daemon_home(monkeypatch):
    monkeypatch.delenv("JARVIS_DAEMON_HOME", raising=False)

    with pytest.raises(RuntimeError):
        env_config.save_settings("sk-new", "voyageai/voyage-4")


def test_read_saved_settings_round_trips_what_was_saved(tmp_path):
    env_config.save_settings("sk-roundtrip", "a-model", "https://example.test", daemon_home=tmp_path)

    assert env_config.read_saved_settings(tmp_path) == {
        "EMBEDDING_API_KEY": "sk-roundtrip",
        "EMBEDDING_MODEL": "a-model",
        "EMBEDDING_API_BASE": "https://example.test",
    }


def test_read_saved_settings_returns_empty_dict_when_nothing_saved_yet(tmp_path):
    assert env_config.read_saved_settings(tmp_path / "nothing-here") == {}


# ---------------------------------------------------------------------
# The daemon's own startup path (jarvis_server.daemon.server calls
# `load_env(_DAEMON_HOME)` at import time, before any other module that
# reads os.environ is imported) - confirms it picks up a key saved at
# exactly that location, via the same function/argument server.py
# actually uses, not a separate, parallel check.
# ---------------------------------------------------------------------

def test_daemon_startup_env_loading_picks_up_a_key_saved_at_daemon_home(tmp_path):
    env_config.save_settings("sk-for-daemon", "voyageai/voyage-4", daemon_home=tmp_path)
    # save_settings() already applied it directly to os.environ (exactly
    # like the settings dialog's own save does) - clear that first, to
    # prove the *file* is what gets picked up here, not a leftover
    # in-process value from the line above.
    del os.environ["EMBEDDING_API_KEY"]
    del os.environ["EMBEDDING_MODEL"]

    env_config.load_env(tmp_path)  # exactly what server.py calls at import time

    assert os.environ["EMBEDDING_API_KEY"] == "sk-for-daemon"
    assert os.environ["EMBEDDING_MODEL"] == "voyageai/voyage-4"


# ---------------------------------------------------------------------
# default_daemon_home()
# ---------------------------------------------------------------------

def test_default_daemon_home_on_windows(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("APPDATA", r"C:\Users\someone\AppData\Roaming")

    assert env_config.default_daemon_home() == r"C:\Users\someone\AppData\Roaming\Jarvis\Jarvis"


def test_default_daemon_home_none_when_appdata_unset(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.delenv("APPDATA", raising=False)

    assert env_config.default_daemon_home() is None


def test_default_daemon_home_on_macos(monkeypatch):
    # Compared via Path, not a hand-written forward-slash string - see
    # the equivalent macOS test in test_claude_desktop_setup.py for why:
    # pathlib always renders with backslashes on this Windows dev
    # machine, regardless of what sys.platform is monkeypatched to.
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(env_config.Path, "home", lambda: env_config.Path("/Users/someone"))

    expected = env_config.Path("/Users/someone") / "Library" / "Application Support" / "Jarvis"
    assert env_config.default_daemon_home() == str(expected)


def test_default_daemon_home_none_on_unsupported_platform(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")

    assert env_config.default_daemon_home() is None


def test_telemetry_toggle_is_saved_and_switches_telemetry_off(tmp_path, monkeypatch):
    from jarvis_server.internal.telemetry.config import resolve_config

    monkeypatch.delenv("JARVIS_TELEMETRY", raising=False)
    monkeypatch.setenv("JARVIS_POSTHOG_KEY", "phc_test")
    assert env_config.telemetry_enabled()

    written = env_config.save_telemetry_enabled(False, daemon_home=tmp_path)

    assert "JARVIS_TELEMETRY" in written.read_text()
    assert not env_config.telemetry_enabled()
    assert resolve_config() is None

    env_config.save_telemetry_enabled(True, daemon_home=tmp_path)
    assert env_config.telemetry_enabled()
    assert resolve_config() is not None


def test_a_frozen_build_never_searches_the_current_directory_for_a_dotenv(monkeypatch, tmp_path):
    searched = []

    def _recording_load_dotenv(dotenv_path=None, *args, **kwargs):
        searched.append(dotenv_path)
        return False

    monkeypatch.setattr(env_config, "load_dotenv", _recording_load_dotenv)
    monkeypatch.setattr(sys, "frozen", True, raising=False)

    env_config.load_env(daemon_home=tmp_path)

    assert searched == [tmp_path / ".env"]
