-- Users, magic-link login requests, sessions, and feedback reports.
--
-- Tokens are never stored in plaintext - only a SHA-256 hash (see
-- auth/tokens.py). These are high-entropy random tokens, not
-- user-chosen passwords, so a slow KDF (bcrypt/argon2) would cost real
-- server CPU per verification for no security benefit; a fast hash is
-- the standard choice for opaque bearer/one-time tokens.
--
-- Like jarvis_server's own SQLite schema, relationships are enforced at
-- the application layer for the hot paths (single-use token consumption
-- is a guarded UPDATE, not a trigger) - foreign keys exist here purely
-- for referential integrity (a user row can't be deleted out from under
-- a session), not as the enforcement mechanism for auth invariants.

CREATE EXTENSION IF NOT EXISTS citext;
CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE users (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email      CITEXT UNIQUE NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- `id` doubles as the `request_id` the desktop app polls with
-- (GET /auth/status?request_id=...). `consumed_at IS NOT NULL` is the
-- single-use guard - /auth/verify sets confirmed_at and consumed_at
-- together in one `UPDATE ... WHERE id = $1 AND consumed_at IS NULL
-- RETURNING *`, so a raced double-click on the emailed link can only
-- ever confirm once.
-- session_delivered_at is a second single-use guard, same shape as
-- consumed_at: the session's raw token only ever exists in memory
-- between being minted and being handed back over one /auth/status
-- poll response (only its hash is ever persisted, in `sessions`) - this
-- column is what lets exactly one poll claim "mint it and hand it back
-- now" via the same guarded-UPDATE pattern as confirm_and_consume,
-- instead of every subsequent poll re-minting or re-delivering it.
CREATE TABLE login_requests (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email               CITEXT NOT NULL,
    token_hash          BYTEA NOT NULL,
    expires_at          TIMESTAMPTZ NOT NULL,
    confirmed_at        TIMESTAMPTZ,
    consumed_at         TIMESTAMPTZ,
    session_delivered_at TIMESTAMPTZ,
    user_id             UUID REFERENCES users(id),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Rate-limiting a single email's /auth/request volume.
CREATE INDEX login_requests_email_created_at_idx ON login_requests (email, created_at);

CREATE TABLE sessions (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id    UUID NOT NULL REFERENCES users(id),
    token_hash BYTEA NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    revoked_at TIMESTAMPTZ
);

-- The /feedback bearer-token lookup path.
CREATE UNIQUE INDEX sessions_token_hash_idx ON sessions (token_hash);

CREATE TABLE feedback (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id          UUID NOT NULL REFERENCES users(id),
    body             TEXT NOT NULL,
    github_issue_url TEXT,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
