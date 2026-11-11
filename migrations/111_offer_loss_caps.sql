-- Offer loss caps (owner, 2026-11-11): a branch offer puts chosen products on
-- Quick Sale for a period, and each customer may take at most a hidden amount of
-- loss (normal price - offer price) from it. docs/plans/offer-loss-cap.md.
-- Additive and replay-safe: every deploy re-applies every file.

CREATE TABLE IF NOT EXISTS offer_caps (
    offer_id    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    branch_id   UUID NOT NULL REFERENCES branches(branch_id) ON DELETE CASCADE,
    name        VARCHAR(120) NOT NULL,
    cap_amount  NUMERIC(10, 2) NOT NULL CHECK (cap_amount > 0),
    starts_at   TIMESTAMPTZ NOT NULL,
    ends_at     TIMESTAMPTZ NOT NULL,
    status      VARCHAR(16) NOT NULL DEFAULT 'scheduled'
                CHECK (status IN ('scheduled', 'live', 'ended', 'cancelled')),
    applied_at  TIMESTAMPTZ,
    restored_at TIMESTAMPTZ,
    created_by  UUID,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (ends_at > starts_at)
);
CREATE INDEX IF NOT EXISTS ix_offer_caps_branch_status ON offer_caps(branch_id, status);

CREATE TABLE IF NOT EXISTS offer_cap_items (
    offer_id                 UUID NOT NULL REFERENCES offer_caps(offer_id) ON DELETE CASCADE,
    product_id               UUID NOT NULL REFERENCES products(product_id) ON DELETE CASCADE,
    offer_price              NUMERIC(10, 2) NOT NULL CHECK (offer_price > 0),
    prev_branch_price        NUMERIC(10, 2),
    prev_discount_percentage DOUBLE PRECISION,
    prev_is_on_sale          BOOLEAN,
    PRIMARY KEY (offer_id, product_id)
);

CREATE TABLE IF NOT EXISTS offer_cap_usage (
    usage_id    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    offer_id    UUID NOT NULL REFERENCES offer_caps(offer_id) ON DELETE CASCADE,
    user_id     UUID NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    order_id    UUID NOT NULL REFERENCES orders(order_id) ON DELETE CASCADE,
    amount      NUMERIC(10, 2) NOT NULL CHECK (amount >= 0),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    released_at TIMESTAMPTZ
);
-- Same names as the ORM model (app/models/offer_cap.py): deploys run
-- create_all first, so whichever creates them, there is one of each.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'uq_offer_cap_usage_offer_order') THEN
        ALTER TABLE offer_cap_usage
            ADD CONSTRAINT uq_offer_cap_usage_offer_order UNIQUE (offer_id, order_id);
    END IF;
END $$;
CREATE INDEX IF NOT EXISTS ix_offer_cap_usage_user_offer ON offer_cap_usage(user_id, offer_id);
CREATE INDEX IF NOT EXISTS ix_offer_cap_usage_order ON offer_cap_usage(order_id);
