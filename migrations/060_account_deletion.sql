-- =============================================================================
-- Migration 060: a customer can close their account, and we keep the record.
--
-- Google Play requires an in-app way to delete an account for any app that
-- lets you create one. This is that, and it is a SOFT delete for a reason
-- that is not laziness.
--
-- The NIC is this business's identity key. If closing an account erased what
-- that NIC had done, anyone who cancelled orders on a loop, or ran up
-- referral clawbacks, could close and reopen and arrive clean. The cancellation
-- review screen would show a first-time customer every time. So the row stays,
-- the NIC stays, and the history stays attached to it.
--
-- What goes is everything that makes the account usable and everything that is
-- personal without being part of the business record:
--
--   * the account cannot be logged into              (is_active = false)
--   * the phone numbers and email are released       (NULL, so the person can
--     register again — and so those numbers are free for their real owner)
--   * push tokens are deleted outright               (no notification may ever
--     reach a closed account)
--   * sessions are revoked                           (a live token must not
--     outlive the account)
--
-- What stays: the NIC, the order history, the money ledger, the cancellation
-- record. Orders are financial records with tax and courier obligations
-- attached; they do not belong to the account, they belong to the transaction.
--
-- `deleted_at` is the switch every query must respect, and `deletion_reason`
-- exists because "why did you leave" is worth asking once.
--
-- SQLAlchemy create_all() adds these on a fresh DB. Idempotent -- the deploy
-- applies this file again on each release.
-- =============================================================================

ALTER TABLE users ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ;
ALTER TABLE users ADD COLUMN IF NOT EXISTS deletion_reason TEXT;

-- The phone and email they held when they closed it. Kept so support can
-- answer "this number used to be mine" and so a re-registration can be
-- recognised as the same person -- but out of the unique columns, so the
-- numbers are genuinely free to be used again.
ALTER TABLE users ADD COLUMN IF NOT EXISTS deleted_phone VARCHAR(20);
ALTER TABLE users ADD COLUMN IF NOT EXISTS deleted_email VARCHAR(255);

-- Every customer-facing query filters on this, so it earns an index. Partial:
-- closed accounts are the small set, and the index stays tiny.
CREATE INDEX IF NOT EXISTS ix_users_deleted
    ON users (deleted_at)
    WHERE deleted_at IS NOT NULL;
