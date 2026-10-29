-- New-customer benefits are immutable snapshots. No existing customer backfill.
BEGIN;
ALTER TABLE orders ADD COLUMN IF NOT EXISTS referral_delivery_discount NUMERIC(10,2) NOT NULL DEFAULT 0;
CREATE TABLE IF NOT EXISTS referral_promotions (
 promotion_id UUID PRIMARY KEY, name VARCHAR(120) NOT NULL,
 partner_id UUID REFERENCES partners(partner_id) ON DELETE RESTRICT,
 scope VARCHAR(20) NOT NULL DEFAULT 'all', rules JSONB NOT NULL,
 starts_at TIMESTAMPTZ NOT NULL, ends_at TIMESTAMPTZ NOT NULL,
 active BOOLEAN NOT NULL DEFAULT FALSE, max_customers INTEGER NOT NULL,
 enrolled_count INTEGER NOT NULL DEFAULT 0, delivery_budget NUMERIC(14,2) NOT NULL DEFAULT 0,
 delivery_spent NUMERIC(14,2) NOT NULL DEFAULT 0, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
 CHECK(scope IN ('all','partner','master_team')), CHECK(max_customers > 0),
 CHECK(enrolled_count >= 0), CHECK(delivery_spent >= 0), CHECK(ends_at > starts_at)
);
CREATE TABLE IF NOT EXISTS referral_benefits (
 benefit_id UUID PRIMARY KEY, user_id UUID NOT NULL UNIQUE REFERENCES users(user_id) ON DELETE RESTRICT,
 promotion_id UUID NOT NULL REFERENCES referral_promotions(promotion_id) ON DELETE RESTRICT,
 partner_id UUID NOT NULL REFERENCES partners(partner_id) ON DELETE RESTRICT,
 title VARCHAR(120) NOT NULL, rules JSONB NOT NULL, expires_at TIMESTAMPTZ NOT NULL,
 used_deliveries INTEGER NOT NULL DEFAULT 0,
 coupon_id UUID REFERENCES coupons(coupon_id) ON DELETE RESTRICT, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
 CHECK(used_deliveries >= 0)
);
CREATE TABLE IF NOT EXISTS referral_delivery_redemptions (
 order_id UUID PRIMARY KEY REFERENCES orders(order_id) ON DELETE RESTRICT,
 benefit_id UUID NOT NULL REFERENCES referral_benefits(benefit_id) ON DELETE RESTRICT,
 amount NUMERIC(12,2) NOT NULL, released BOOLEAN NOT NULL DEFAULT FALSE,
 created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), CHECK(amount > 0)
);
COMMIT;
