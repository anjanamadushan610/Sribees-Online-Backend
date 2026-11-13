-- =============================================================================
-- Migration 116: tell customers about a coupon when it starts (owner, 2026-11-13).
--
-- The admin form's "notify" panel was never implemented server-side. Now:
--   * coupons.notify_customers -- the admin's switch (default off);
--   * coupons.announced_at     -- an "All customers" / members-only coupon is
--                                 announced once; set when that sweep finished;
--   * coupon_announcements     -- one push per customer per coupon, ever. The
--                                 primary key is the dedupe: the row is written
--                                 before the push, so a retried sweep or a second
--                                 API worker never sends twice.
--
-- Additive and replay-safe: every existing coupon stays silent.
-- =============================================================================

ALTER TABLE coupons ADD COLUMN IF NOT EXISTS notify_customers BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE coupons ADD COLUMN IF NOT EXISTS announced_at TIMESTAMPTZ;

CREATE TABLE IF NOT EXISTS coupon_announcements (
    coupon_id UUID NOT NULL REFERENCES coupons(coupon_id) ON DELETE CASCADE,
    user_id   UUID NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    sent_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (coupon_id, user_id)
);
