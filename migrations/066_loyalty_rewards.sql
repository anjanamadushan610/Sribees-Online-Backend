-- =============================================================================
-- 066_loyalty_rewards.sql — Cashback waits for the return window; loyalty
-- points and levels.
--
-- Until now an order's cashback went into the wallet the moment the order was
-- placed (or paid, for card orders). The customer could spend it the same
-- day, cancel or return the goods, and be clawed back into a negative wallet.
-- Returned goods also kept their cashback: nothing reduced it.
--
-- `order_rewards` holds what an order will earn -- its cashback and the
-- loyalty points for its value -- until the goods are delivered and the
-- twelve-hour return window has closed, the same moment a referral commission
-- vests (058). A sweeper then credits the wallet and adds the points, once:
-- the row's status and wallet_transaction_id prove it settled.
--
-- Points never go down. `loyalty_accounts.lifetime_points` only grows, and
-- the level a customer is at is read from it against `loyalty_tiers`, which
-- the Super Admin edits. `spend_carry` keeps the rupees short of the next
-- point, so Rs 700 + Rs 700 still earns one point at Rs 1000 a point.
--
-- A level may carry a reward coupon issued once to each customer who reaches
-- it (`loyalty_tier_rewards` is the once), and a coupon may require a minimum
-- level (`coupons.min_tier_id`).
--
-- Idempotent: the deploy applies this file again on every release.
-- =============================================================================

CREATE TABLE IF NOT EXISTS loyalty_tiers (
    tier_id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name                   VARCHAR(50)  NOT NULL,
    name_si                VARCHAR(50),
    name_ta                VARCHAR(50),
    min_points             INTEGER      NOT NULL CHECK (min_points >= 0),
    color                  VARCHAR(7),
    is_active              BOOLEAN      NOT NULL DEFAULT TRUE,
    -- The coupon a customer is given on reaching this level. NULL type = none.
    reward_discount_type   VARCHAR(20)  CHECK (reward_discount_type IN ('percentage', 'fixed')),
    reward_discount_value  NUMERIC(10, 2) CHECK (reward_discount_value > 0),
    reward_min_order_value NUMERIC(10, 2) NOT NULL DEFAULT 0 CHECK (reward_min_order_value >= 0),
    reward_max_discount    NUMERIC(10, 2) CHECK (reward_max_discount > 0),
    reward_valid_days      INTEGER      CHECK (reward_valid_days > 0),
    created_at             TIMESTAMPTZ  DEFAULT now(),
    updated_at             TIMESTAMPTZ  DEFAULT now(),
    CONSTRAINT uq_loyalty_tiers_min_points UNIQUE (min_points),
    CONSTRAINT ck_loyalty_tiers_reward_complete CHECK (
        reward_discount_type IS NULL
        OR (reward_discount_value IS NOT NULL AND reward_valid_days IS NOT NULL)
    )
);

-- Seeded once, only into an empty table, so an admin's edits survive every
-- redeploy. At Rs 1,000 a point: Silver after Rs 25,000 of kept purchases,
-- Gold after Rs 100,000.
INSERT INTO loyalty_tiers (name, name_si, name_ta, min_points, color,
                           reward_discount_type, reward_discount_value,
                           reward_min_order_value, reward_max_discount, reward_valid_days)
SELECT * FROM (VALUES
    ('Member', 'සාමාජික', 'உறுப்பினர்', 0,   '#8A8FA3', NULL,         NULL::NUMERIC,  0::NUMERIC,    NULL::NUMERIC,  NULL::INTEGER),
    ('Silver', 'රිදී',     'வெள்ளி',     25,  '#9AA5B1', 'percentage', 5::NUMERIC,     2000::NUMERIC, 300::NUMERIC,   30),
    ('Gold',   'රන්',      'தங்கம்',     100, '#C9A227', 'percentage', 10::NUMERIC,    3000::NUMERIC, 1000::NUMERIC,  30)
) AS seed(name, name_si, name_ta, min_points, color,
          reward_discount_type, reward_discount_value,
          reward_min_order_value, reward_max_discount, reward_valid_days)
WHERE NOT EXISTS (SELECT 1 FROM loyalty_tiers);

CREATE TABLE IF NOT EXISTS loyalty_accounts (
    user_id          UUID PRIMARY KEY REFERENCES users(user_id) ON DELETE CASCADE,
    lifetime_points  INTEGER        NOT NULL DEFAULT 0 CHECK (lifetime_points >= 0),
    -- Rupees earned toward the next point, always below one point's worth.
    spend_carry      NUMERIC(12, 2) NOT NULL DEFAULT 0 CHECK (spend_carry >= 0),
    lifetime_spend   NUMERIC(14, 2) NOT NULL DEFAULT 0 CHECK (lifetime_spend >= 0),
    created_at       TIMESTAMPTZ    DEFAULT now(),
    updated_at       TIMESTAMPTZ    DEFAULT now()
);

CREATE TABLE IF NOT EXISTS order_rewards (
    reward_id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    order_id              UUID NOT NULL REFERENCES orders(order_id) ON DELETE CASCADE,
    order_number          VARCHAR(50),
    user_id               UUID NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    -- What the order earns now; lowered as goods come back.
    cashback_amount       NUMERIC(10, 2) NOT NULL DEFAULT 0 CHECK (cashback_amount >= 0),
    status                VARCHAR(20)    NOT NULL DEFAULT 'pending',
    -- Delivery + the return window + a minute; NULL until delivered.
    vests_at              TIMESTAMPTZ,
    settled_at            TIMESTAMPTZ,
    reversed_at           TIMESTAMPTZ,
    -- The order value the points were counted on, and how many they were.
    qualifying_amount     NUMERIC(12, 2),
    points_awarded        INTEGER        NOT NULL DEFAULT 0 CHECK (points_awarded >= 0),
    wallet_transaction_id UUID REFERENCES wallet_transactions(transaction_id) ON DELETE SET NULL,
    created_at            TIMESTAMPTZ    DEFAULT now(),
    updated_at            TIMESTAMPTZ    DEFAULT now(),
    -- One reward per order: accrual can run twice (a replayed payment
    -- confirmation) without ever paying twice.
    CONSTRAINT uq_order_rewards_order UNIQUE (order_id),
    CONSTRAINT ck_order_rewards_status CHECK (status IN ('pending', 'settled', 'reversed'))
);

CREATE INDEX IF NOT EXISTS ix_order_rewards_user_status
    ON order_rewards (user_id, status);

-- The sweeper's only query. Partial, so a row leaves the index once it settles.
CREATE INDEX IF NOT EXISTS ix_order_rewards_due
    ON order_rewards (vests_at)
    WHERE status = 'pending' AND vests_at IS NOT NULL;

CREATE TABLE IF NOT EXISTS loyalty_tier_rewards (
    user_id    UUID NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    tier_id    UUID NOT NULL REFERENCES loyalty_tiers(tier_id) ON DELETE CASCADE,
    coupon_id  UUID REFERENCES coupons(coupon_id) ON DELETE SET NULL,
    issued_at  TIMESTAMPTZ DEFAULT now(),
    PRIMARY KEY (user_id, tier_id)
);

-- RESTRICT, not SET NULL: deleting a level must never quietly open its
-- members-only coupons to everyone.
ALTER TABLE coupons
    ADD COLUMN IF NOT EXISTS min_tier_id UUID
        REFERENCES loyalty_tiers(tier_id) ON DELETE RESTRICT;

-- Every order that predates this file, except cancelled ones and card orders
-- never paid. Those already credited at checkout carry cashback_credited_at,
-- and settling checks it, so they earn their points without a second credit.
INSERT INTO order_rewards (order_id, order_number, user_id, cashback_amount, status, vests_at)
SELECT o.order_id,
       o.order_number,
       o.user_id,
       GREATEST(COALESCE(o.cashback_earned, 0), 0),
       'pending',
       CASE WHEN o.delivered_at IS NOT NULL
            THEN o.delivered_at + INTERVAL '12 hours 1 minute' END
FROM   orders o
WHERE  o.status <> 'cancelled'
  AND  (o.payment_method NOT IN ('MARX_OTHER', 'MARX_AMEX', 'MARX_BANK')
        OR o.payment_status IN ('paid', 'refunded'))
ON CONFLICT (order_id) DO NOTHING;
