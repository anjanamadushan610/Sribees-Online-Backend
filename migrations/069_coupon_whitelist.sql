-- =============================================================================
-- Migration 069: a "Specific Customers" coupon is actually limited to them.
--
-- The admin coupon form has offered "Specific Customers (Whitelist)" with CSV
-- import, but nothing stored the list or the choice: the server received
-- them and dropped them, so a coupon made for five customers was usable by
-- every customer who had the code, and shown to all of them if public.
--
-- `target_customer_type` records the choice. 'specific' with no rows admits
-- nobody, so a list that failed to save can never read as "everyone".
-- Numbers are stored in canonical +94 form, which is what lets a customer who
-- signs up with a listed number later still be recognised.
--
-- Idempotent -- the deploy applies this file again on each release.
-- =============================================================================

ALTER TABLE coupons
    ADD COLUMN IF NOT EXISTS target_customer_type VARCHAR(20) NOT NULL DEFAULT 'all';

-- A members-only coupon from before this column existed is a 'tier' coupon.
UPDATE coupons SET target_customer_type = 'tier'
    WHERE min_tier_id IS NOT NULL AND target_customer_type = 'all';

CREATE TABLE IF NOT EXISTS coupon_whitelist (
    coupon_id UUID NOT NULL REFERENCES coupons(coupon_id) ON DELETE CASCADE,
    phone     VARCHAR(20) NOT NULL,
    name      VARCHAR(255),
    user_id   UUID REFERENCES users(user_id) ON DELETE SET NULL,
    PRIMARY KEY (coupon_id, phone)
);

CREATE INDEX IF NOT EXISTS ix_coupon_whitelist_phone ON coupon_whitelist (phone);
