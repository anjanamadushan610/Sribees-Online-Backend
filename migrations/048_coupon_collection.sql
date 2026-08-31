-- =============================================================================
-- Migration 048: Collectible coupons + per-customer limits
--
--   * coupons.is_public       — listed in the mobile "Available Offers" feed;
--                               must be collected before it can be applied.
--   * coupons.per_user_limit  — max redemptions per customer (NULL = unlimited;
--                               default 1). Enforced via user_coupons.
--   * user_coupons            — one row per (customer, coupon): collected in the
--                               app, or auto-created on first redemption of a
--                               private code. times_redeemed is checked against
--                               per_user_limit.
--
-- SQLAlchemy create_all() covers a fresh DB; this file is for pre-existing
-- databases. Idempotent — safe to re-run.
-- =============================================================================

ALTER TABLE coupons ADD COLUMN IF NOT EXISTS is_public BOOLEAN NOT NULL DEFAULT FALSE;
-- No DEFAULT: pre-existing coupons stay NULL (= unlimited per customer), so this
-- migration never silently caps a live promo. New coupons created through the
-- admin API default to 1 (see app/schemas/coupon.py).
ALTER TABLE coupons ADD COLUMN IF NOT EXISTS per_user_limit INTEGER;

CREATE TABLE IF NOT EXISTS user_coupons (
    user_coupon_id        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id               UUID NOT NULL REFERENCES users(user_id)   ON DELETE CASCADE,
    coupon_id             UUID NOT NULL REFERENCES coupons(coupon_id) ON DELETE CASCADE,
    collected_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    times_redeemed        INTEGER NOT NULL DEFAULT 0,
    last_redeemed_at      TIMESTAMPTZ,
    last_redeemed_order_id UUID REFERENCES orders(order_id) ON DELETE SET NULL,
    CONSTRAINT uq_user_coupons_user_coupon UNIQUE (user_id, coupon_id)
);

-- Real the default on a DB where create_all() built the table first (the ORM
-- declares the PK default Python-side, leaving the column with no DB default).
ALTER TABLE user_coupons ALTER COLUMN user_coupon_id SET DEFAULT gen_random_uuid();

CREATE INDEX IF NOT EXISTS idx_user_coupons_user   ON user_coupons (user_id);
CREATE INDEX IF NOT EXISTS idx_user_coupons_coupon ON user_coupons (coupon_id);
CREATE INDEX IF NOT EXISTS idx_coupons_public
    ON coupons (is_public) WHERE is_public = TRUE;
