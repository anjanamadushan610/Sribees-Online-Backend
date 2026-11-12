-- Admin refresh grace (2026-11-12): the Price app on a shop floor can lose the
-- response of a refresh call (weak signal, app killed). The server had already
-- rotated the refresh token, so the phone kept the old one and was logged out
-- on its next refresh. Keep the previous token's hash for a short grace window
-- so one retry with it still works. Additive and replay-safe.

ALTER TABLE admin_sessions ADD COLUMN IF NOT EXISTS previous_refresh_token_hash VARCHAR(255);
ALTER TABLE admin_sessions ADD COLUMN IF NOT EXISTS rotated_at TIMESTAMPTZ;
