-- =============================================================================
-- Migration 115: a "Specific Customers" coupon can list a customer by account.
--
-- coupon_whitelist was keyed on (coupon_id, phone), so a customer who signed
-- in with Google or Apple and never gave a mobile number could not be listed
-- at all. The admin now picks customers from a search, which knows the
-- account, so a row is a phone, an account, or both:
--
--   * entry_id becomes the primary key;
--   * phone may be NULL when the row names an account;
--   * a row must name at least one of the two;
--   * a number is still listed at most once per coupon, and an account-only
--     row at most once per coupon.
--
-- Checkout already admitted a listed account (CouponService.whitelist_allows
-- matches user_id first), so nothing changes there.
--
-- Idempotent -- the deploy applies this file again on each release.
-- =============================================================================

ALTER TABLE coupon_whitelist
    ADD COLUMN IF NOT EXISTS entry_id UUID NOT NULL DEFAULT gen_random_uuid();

-- Swap the key exactly once. The new key has its own name, so a replay finds
-- the old one gone and does nothing.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'coupon_whitelist'::regclass
          AND conname = 'coupon_whitelist_pkey'
    ) THEN
        ALTER TABLE coupon_whitelist DROP CONSTRAINT coupon_whitelist_pkey;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'coupon_whitelist'::regclass
          AND conname = 'coupon_whitelist_entry_pkey'
    ) THEN
        ALTER TABLE coupon_whitelist
            ADD CONSTRAINT coupon_whitelist_entry_pkey PRIMARY KEY (entry_id);
    END IF;
END $$;

ALTER TABLE coupon_whitelist ALTER COLUMN phone DROP NOT NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'coupon_whitelist'::regclass
          AND conname = 'ck_coupon_whitelist_phone_or_user'
    ) THEN
        ALTER TABLE coupon_whitelist
            ADD CONSTRAINT ck_coupon_whitelist_phone_or_user
            CHECK (phone IS NOT NULL OR user_id IS NOT NULL);
    END IF;
END $$;

-- The old primary key guaranteed this, so existing rows already satisfy it.
CREATE UNIQUE INDEX IF NOT EXISTS ux_coupon_whitelist_coupon_phone
    ON coupon_whitelist (coupon_id, phone) WHERE phone IS NOT NULL;

-- No account-only rows exist before this file, so this cannot fail on old data.
CREATE UNIQUE INDEX IF NOT EXISTS ux_coupon_whitelist_coupon_account
    ON coupon_whitelist (coupon_id, user_id) WHERE phone IS NULL;

CREATE INDEX IF NOT EXISTS ix_coupon_whitelist_coupon ON coupon_whitelist (coupon_id);
