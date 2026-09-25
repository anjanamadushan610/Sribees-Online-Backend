-- =============================================================================
-- Migration 070: a guest's support tickets belong to the device that sent them.
--
-- GET /support/my-tickets?phone=... answered anyone who supplied a phone
-- number -- no login, no proof -- with every ticket filed under it: the
-- customer's name, what they wrote to support, and staff's resolution notes.
-- A phone number is not a secret, so that was a lookup of other people's
-- support conversations.
--
-- A guest has no account to prove, but the app sends a stable per-install
-- X-Device-Id on every guest request. Recording it on the ticket lets the
-- guest lookup require BOTH the phone and the install that filed it. Signed-in
-- customers are matched by their own account and never by a supplied number.
--
-- Guest tickets filed before this column existed have no device and stop
-- appearing in guest lookups; they still reach support staff as before, and
-- appear for the customer once they sign in with that phone number.
--
-- Idempotent -- the deploy applies this file again on each release.
-- =============================================================================

ALTER TABLE support_tickets
    ADD COLUMN IF NOT EXISTS device_id VARCHAR(128);

CREATE INDEX IF NOT EXISTS ix_support_tickets_device_id ON support_tickets (device_id);
