"""Magic-link / session token generation and hashing.

Tokens are high-entropy random strings, not user-chosen passwords - a
fast cryptographic hash (SHA-256) is the standard, correct choice here,
not a slow password KDF (bcrypt/argon2), which would only add server CPU
cost per verification with no security benefit for this kind of secret.
"""

import hashlib
import secrets


def generate_token() -> str:
    """A URL-safe, single-use, unguessable token - used both for the
    magic-link itself and for the session bearer token handed back once
    it's confirmed."""
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> bytes:
    """The only form a token is ever stored in - see module docstring."""
    return hashlib.sha256(token.encode("utf-8")).digest()
