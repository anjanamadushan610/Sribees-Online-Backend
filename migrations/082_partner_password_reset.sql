-- =============================================================================
-- 082_partner_password_reset.sql -- Partner portal "forgot password".
--
-- The reset code itself lives in Redis (5 minutes, hashed). The database only
-- records when a partner's password last changed, so get_current_partner can
-- refuse every token issued before it: a reset (or an admin setting a new
-- password) signs the partner out everywhere.
--
-- Idempotent: safe to re-run on every deploy.
-- =============================================================================

BEGIN;

ALTER TABLE partners
    ADD COLUMN IF NOT EXISTS password_changed_at TIMESTAMPTZ;

COMMENT ON COLUMN partners.password_changed_at IS
    'Tokens issued before this are refused (get_current_partner).';

COMMIT;
