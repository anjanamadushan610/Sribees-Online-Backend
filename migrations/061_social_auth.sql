-- =============================================================================
-- Migration 061: signing in with Google or Apple.
--
-- Both go through Firebase Auth, which the server already trusts -- the Admin
-- SDK is initialised for push, and the same credential verifies an ID token.
-- One verification path for both providers, and no second copy of Apple's JWKS
-- handling to keep correct.
--
-- `provider_uid` is Firebase's stable uid for the account, not the email. An
-- email can be changed, released and re-registered by somebody else; the uid
-- cannot, so it is what an account is recognised by on the second sign-in.
--
-- The UNIQUE index is partial so the millions of phone-registered rows, which
-- have no provider, do not collide with each other on NULL.
--
-- The NIC is untouched by any of this. It stays unique, it stays write-once,
-- and it is still collected at the profile gate -- a Google sign-in gets an
-- account, not an exemption.
--
-- SQLAlchemy create_all() adds these on a fresh DB. Idempotent.
-- =============================================================================

ALTER TABLE users ADD COLUMN IF NOT EXISTS auth_provider VARCHAR(20);
ALTER TABLE users ADD COLUMN IF NOT EXISTS provider_uid  VARCHAR(128);

CREATE UNIQUE INDEX IF NOT EXISTS ix_users_provider_uid_unique
    ON users (provider_uid)
    WHERE provider_uid IS NOT NULL;
