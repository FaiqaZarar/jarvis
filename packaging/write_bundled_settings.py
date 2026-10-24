"""Write jarvis_server/internal/_bundled_credentials.py for a release build.

Run by .github/workflows/build.yml before PyInstaller. Every value comes from
an optional environment variable (a repository secret in CI); a missing one
is written as "" and that feature is simply off in the build. There is
deliberately no embedding API key here: one baked into a public installer
could be extracted by anyone, so each user adds their own on first run.
"""

import os
from pathlib import Path

TARGET = Path(__file__).resolve().parent.parent / "jarvis" / "server" / "jarvis_server" / "internal" / "_bundled_credentials.py"

SETTINGS = {
    "BUNDLED_GOOGLE_OAUTH_CLIENT_ID": "GOOGLE_OAUTH_CLIENT_ID",
    "BUNDLED_GOOGLE_OAUTH_CLIENT_SECRET": "GOOGLE_OAUTH_CLIENT_SECRET",
    "BUNDLED_EMBEDDING_API_KEY": None,
    "BUNDLED_POSTHOG_PROJECT_KEY": "POSTHOG_PROJECT_KEY",
    "BUNDLED_POSTHOG_HOST": "POSTHOG_HOST",
    "BUNDLED_CLOUD_BASE_URL": "CLOUD_BASE_URL",
}

values = {name: os.environ.get(env, "") if env else "" for name, env in SETTINGS.items()}
TARGET.write_text("".join(f"{name} = {value!r}\n" for name, value in values.items()), encoding="utf-8")
print("Build settings:", ", ".join(f"{name}={'on' if value else 'off'}" for name, value in values.items()))
