-- =============================================================================
-- 083_apple_refresh_token.sql -- Revoke Sign in with Apple on account closure.
--
-- App Store guideline 5.1.1(v): closing an account that signed in with Apple
-- must also revoke the app's authorization at Apple. That needs the refresh
-- token Apple issues for the sign-in's authorization code, and the client
-- (bundle ID or Services ID) it was issued to. Both are cleared when the
-- account closes.
--
-- Idempotent: safe to re-run on every deploy.
-- =============================================================================

BEGIN;

ALTER TABLE users
    ADD COLUMN IF NOT EXISTS apple_refresh_token TEXT,
    ADD COLUMN IF NOT EXISTS apple_client_id VARCHAR(255);

COMMENT ON COLUMN users.apple_refresh_token IS
    'Sign in with Apple refresh token; only used to revoke it on account closure.';

COMMIT;
