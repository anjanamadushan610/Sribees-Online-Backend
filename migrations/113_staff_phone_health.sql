-- Staff phone health (2026-11-12, New Orders P9): each registration also says
-- whether the phone can actually ring -- notifications allowed, the New
-- orders channel on (not off or silent), not battery-restricted -- plus the app
-- version and model, so the owner can see which staff phones would stay quiet.
-- All nullable: apps older than 0.8.0 send none of them ("unknown").
-- Additive and replay-safe: every deploy re-applies every file.

ALTER TABLE admin_push_tokens ADD COLUMN IF NOT EXISTS notifications_on BOOLEAN;
ALTER TABLE admin_push_tokens ADD COLUMN IF NOT EXISTS channel_state VARCHAR(8);
ALTER TABLE admin_push_tokens ADD COLUMN IF NOT EXISTS background_restricted BOOLEAN;
ALTER TABLE admin_push_tokens ADD COLUMN IF NOT EXISTS app_version VARCHAR(32);
ALTER TABLE admin_push_tokens ADD COLUMN IF NOT EXISTS device_model VARCHAR(64);
