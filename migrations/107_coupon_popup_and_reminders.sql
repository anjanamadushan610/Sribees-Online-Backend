-- Coupon countdown, app-open popup and expiry reminder (2026-11-10).
-- Additive and replay-safe: every existing coupon stays out of the popup.
ALTER TABLE coupons ADD COLUMN IF NOT EXISTS show_in_popup BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE coupons ADD COLUMN IF NOT EXISTS popup_priority INTEGER NOT NULL DEFAULT 0;

-- One expiry reminder per customer per coupon, ever. The primary key is the
-- dedupe: a second worker (or a retried sweep) inserts nothing and sends nothing.
CREATE TABLE IF NOT EXISTS coupon_expiry_reminders (
    coupon_id UUID NOT NULL REFERENCES coupons(coupon_id) ON DELETE CASCADE,
    user_id UUID NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    sent_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (coupon_id, user_id)
);
