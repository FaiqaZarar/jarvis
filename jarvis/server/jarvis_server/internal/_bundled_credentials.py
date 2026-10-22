"""Build-time constants, overwritten by build.yml right before each
platform's PyInstaller invocation - see that workflow for exactly how.

Checked in with harmless empty defaults so a local/dev run - which never
goes through that CI step - just gets `env_config._apply_bundled_google_
oauth_client()`/`_apply_bundled_embedding_key()` doing nothing (see that
module: a real per-tester `.env` value, or nothing at all in dev, both
already work without this). Never edit these values by hand; they only
mean anything as CI's generated version of this file, never as what's
committed here.
"""

BUNDLED_GOOGLE_OAUTH_CLIENT_ID = ""
BUNDLED_GOOGLE_OAUTH_CLIENT_SECRET = ""
BUNDLED_EMBEDDING_API_KEY = ""
BUNDLED_POSTHOG_PROJECT_KEY = ""
BUNDLED_POSTHOG_HOST = ""
BUNDLED_CLOUD_BASE_URL = ""
