-- =============================================================================
-- 094 - Stock levels as Sribees Finance sees them (shadow, not yet in charge)
-- =============================================================================
--
-- Sribees Finance owns stock by batch. After every movement it pushes the
-- sellable quantity per branch and product (unexpired, not quarantined) to
-- PUT /internal/finance/stock-levels, which records it here.
--
-- During the parallel run this table is ONLY a record to compare with
-- branch_inventory; nothing here changes what the shop sells. Letting
-- Finance drive branch_inventory is the go-live switch (DESIGN.md phase 6).
--
-- `version` only moves forward, so a late or replayed push never overwrites
-- a newer one. REPLAYED ON EVERY DEPLOY: idempotent.
-- =============================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS finance_stock_levels (
    branch_id    UUID          NOT NULL,
    product_id   UUID          NOT NULL,
    sellable_qty NUMERIC(14,3) NOT NULL,
    unit         VARCHAR(10)   NOT NULL,
    version      BIGINT        NOT NULL,
    received_at  TIMESTAMPTZ   NOT NULL DEFAULT now(),
    PRIMARY KEY (branch_id, product_id)
);

COMMIT;
