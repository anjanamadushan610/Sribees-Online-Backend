-- =============================================================================
-- Migration 042: Add is_blocked / blocked_reason to users table
--
-- Backs the admin customer-block feature (POST /admin/customers/{id}/block).
-- Previously applied by an ad-hoc ALTER inside app/config/database.py's
-- init_db() at startup instead of a tracked migration — moved here so it goes
-- through the same reviewable, ordered path as every other schema change, and
-- so init_db() no longer runs DDL on every boot.
-- =============================================================================

ALTER TABLE users ADD COLUMN IF NOT EXISTS is_blocked BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE users ADD COLUMN IF NOT EXISTS blocked_reason TEXT NULL;
