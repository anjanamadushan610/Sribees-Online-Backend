-- =============================================================================
-- 102 - Minimum prices from Sribees Finance
-- =============================================================================
--
-- Sribees Finance works out, per item and branch, the lowest selling price that
-- keeps the owner's margin: its ready cost (latest purchase price, with transport
-- and, for repacked goods, bags and labels) divided by (1 - margin - cashback -
-- referral). It pushes the result here. The admin and the Sribees Price app show a
-- quiet icon by a price below it, and Price Intel reads it as its floor.
--
-- Advice only: nothing here changes what a customer pays or can buy.
-- One row per branch and stock item (the product, or a pack size = its variant).
--
-- REPLAYED ON EVERY DEPLOY: idempotent.
-- =============================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS finance_min_prices (
    branch_id        UUID NOT NULL,
    item_id          UUID NOT NULL,
    product_id       UUID NOT NULL,
    variant_id       UUID,
    min_price        NUMERIC(14,2) CHECK (min_price >= 0),
    ready_cost       NUMERIC(14,2) CHECK (ready_cost >= 0),
    purchase_cost    NUMERIC(14,2) CHECK (purchase_cost >= 0),
    materials_cost   NUMERIC(14,2) CHECK (materials_cost >= 0),
    margin_pct       NUMERIC(5,2),
    cashback_pct     NUMERIC(5,2),
    referral_pct     NUMERIC(5,2),
    cost_ref         VARCHAR(120),
    no_floor_reason  VARCHAR(200),
    version          BIGINT NOT NULL,
    received_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (branch_id, item_id)
);
CREATE INDEX IF NOT EXISTS idx_finance_min_prices_product ON finance_min_prices (product_id);

COMMIT;
