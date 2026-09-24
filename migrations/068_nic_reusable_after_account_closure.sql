-- =============================================================================
-- Migration 068: a customer who closed their account can sign up again with
-- the same NIC.
--
-- Closing an account (migration 060) keeps the row and its NIC, so the orders,
-- the wallet ledger and the cancellation record stay attached to the person.
-- But the NIC index counted closed rows too, so the same person coming back
-- was refused at the profile step with "This NIC is already registered to
-- another account" -- by their own closed account.
--
-- The rule is now one LIVE account per NIC. The closed row keeps its NIC and
-- stays visible to admins (the Deleted tab, and "linked accounts" on the
-- profile); the new account starts empty, because it is a new user_id and
-- none of the old orders belong to it.
--
-- Idempotent -- the deploy applies this file again on each release.
-- =============================================================================

-- Every all-rows uniqueness on nic: a constraint if the column was ever
-- declared unique inline (dropped first, since it owns its index), the index
-- from migration 055, and the one SQLAlchemy's create_all() made from the
-- model's former `unique=True, index=True`.
ALTER TABLE users DROP CONSTRAINT IF EXISTS users_nic_key;
DROP INDEX IF EXISTS ix_users_nic_unique;
DROP INDEX IF EXISTS ix_users_nic;

CREATE UNIQUE INDEX IF NOT EXISTS ix_users_nic_live
    ON users (nic)
    WHERE nic IS NOT NULL AND deleted_at IS NULL;
