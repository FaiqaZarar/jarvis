"""Cloud sign-in session storage - the OS Credential Manager/Keychain via
`keyring`, not a plain file (contrast with the Drive OAuth token and the
embedding API key, both plain files with chmod 0600 - see
jarvis_server.connectors.drive.auth and jarvis_server.internal.env_config).
A leaked cloud session token grants access to the whole account, a wider
blast radius than either of those, which is what justifies the extra
dependency here rather than reusing the existing file-based convention.
"""

from typing import Optional

import keyring

_SERVICE_NAME = "jarvis-cloud"
_EMAIL_KEY = "email"
_TOKEN_KEY = "session_token"


def save_session(email: str, token: str) -> None:
    keyring.set_password(_SERVICE_NAME, _EMAIL_KEY, email)
    keyring.set_password(_SERVICE_NAME, _TOKEN_KEY, token)


def load_session() -> Optional[tuple[str, str]]:
    """None if nothing is saved yet, or if only one of the two keys is
    (shouldn't happen outside manual tampering, but treated as "not
    signed in" rather than a half-valid state)."""
    email = keyring.get_password(_SERVICE_NAME, _EMAIL_KEY)
    token = keyring.get_password(_SERVICE_NAME, _TOKEN_KEY)
    if not email or not token:
        return None
    return email, token


def clear_session() -> None:
    for key in (_EMAIL_KEY, _TOKEN_KEY):
        try:
            keyring.delete_password(_SERVICE_NAME, key)
        except keyring.errors.PasswordDeleteError:
            pass
