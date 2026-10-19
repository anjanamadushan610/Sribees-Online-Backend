-- =============================================================================
-- 081_product_referral_commission.sql — Referral commission per product.
--
-- Until now every referred order paid one platform rate on the whole basket.
-- Some goods cannot carry a commission, so the rate now lives on the product
-- (Super Admin default) and on branch_inventory (Branch Manager override),
-- resolved branch -> product -> 0 at checkout and frozen onto order_items the
-- way cashback_percentage is (065). Each commission row is broken into the
-- lines it was earned on, so the partner can see where the money came from and
-- a return takes back exactly what the returned units earned.
--
-- See docs/product-referral-commission.md.
-- Idempotent: safe to re-run on every deploy.
-- =============================================================================

BEGIN;

-- 1. Rates ---------------------------------------------------------------------
ALTER TABLE products
    ADD COLUMN IF NOT EXISTS referral_commission_percentage NUMERIC(5, 2);
ALTER TABLE branch_inventory
    ADD COLUMN IF NOT EXISTS referral_commission_percentage NUMERIC(5, 2);

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'chk_products_referral_pct') THEN
        ALTER TABLE products ADD CONSTRAINT chk_products_referral_pct
            CHECK (referral_commission_percentage IS NULL
                   OR referral_commission_percentage BETWEEN 0 AND 100);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'chk_branch_inv_referral_pct') THEN
        ALTER TABLE branch_inventory ADD CONSTRAINT chk_branch_inv_referral_pct
            CHECK (referral_commission_percentage IS NULL
                   OR referral_commission_percentage BETWEEN 0 AND 100);
    END IF;
END $$;

COMMENT ON COLUMN products.referral_commission_percentage IS
    'Default level-1 referral commission % (0-100). NULL = no commission; a branch may override it.';
COMMENT ON COLUMN branch_inventory.referral_commission_percentage IS
    'Branch override referral commission % (NULL -> product default -> 0).';

-- 2. Checkout snapshot ---------------------------------------------------------
ALTER TABLE order_items
    ADD COLUMN IF NOT EXISTS referral_commission_percentage NUMERIC(5, 2);
ALTER TABLE order_items
    ADD COLUMN IF NOT EXISTS coupon_discount NUMERIC(10, 2) NOT NULL DEFAULT 0;

COMMENT ON COLUMN order_items.referral_commission_percentage IS
    'Level-1 referral commission % resolved at checkout. NULL = placed before 081.';
COMMENT ON COLUMN order_items.coupon_discount IS
    'This line''s share of orders.discount_amount; the lines sum to it exactly.';

-- 3. Commission lines ----------------------------------------------------------
CREATE TABLE IF NOT EXISTS referral_commission_lines (
    line_id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    commission_id     UUID NOT NULL REFERENCES referral_commissions(commission_id) ON DELETE CASCADE,
    order_item_id     UUID REFERENCES order_items(order_item_id) ON DELETE SET NULL,
    product_id        UUID NOT NULL,
    product_name      VARCHAR(255) NOT NULL,
    quantity          INTEGER NOT NULL,
    basis             NUMERIC(10, 2) NOT NULL,
    rate_percentage   NUMERIC(6, 3) NOT NULL,
    amount            NUMERIC(10, 2) NOT NULL,
    returned_quantity INTEGER NOT NULL DEFAULT 0,
    created_at        TIMESTAMPTZ DEFAULT now(),
    CONSTRAINT chk_ref_line_quantity CHECK (quantity > 0),
    CONSTRAINT chk_ref_line_returned CHECK (returned_quantity BETWEEN 0 AND quantity),
    CONSTRAINT chk_ref_line_money CHECK (basis >= 0 AND amount >= 0),
    CONSTRAINT chk_ref_line_rate CHECK (rate_percentage BETWEEN 0 AND 100)
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_ref_line_commission_item
    ON referral_commission_lines (commission_id, order_item_id)
    WHERE order_item_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_ref_line_commission
    ON referral_commission_lines (commission_id);
CREATE INDEX IF NOT EXISTS idx_ref_line_product
    ON referral_commission_lines (product_id);

-- 4. Rate change audit ---------------------------------------------------------
CREATE TABLE IF NOT EXISTS referral_rate_changes (
    change_id      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    product_id     UUID NOT NULL REFERENCES products(product_id) ON DELETE CASCADE,
    branch_id      UUID REFERENCES branches(branch_id) ON DELETE CASCADE,
    old_percentage NUMERIC(5, 2),
    new_percentage NUMERIC(5, 2),
    changed_by     UUID,
    changed_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_ref_rate_changes_product
    ON referral_rate_changes (product_id, changed_at DESC);

-- 5. Programme settings (kept if an admin already changed them) ----------------
INSERT INTO app_settings (setting_id, key, value, description, is_active)
VALUES
    (gen_random_uuid(), 'referral_branch_max_percent', '10',
     'Highest referral commission % a Branch Manager may set on a product.', TRUE),
    (gen_random_uuid(), 'referral_level2_share', '0.5',
     'Fraction of a product''s level-1 commission paid to the partner''s upline.', TRUE)
ON CONFLICT (key) DO NOTHING;

COMMIT;
