-- =============================================================================
-- 023_product_cashback.sql — Per-product, per-branch cashback.
--
-- Cashback used to be one flat platform rate applied to the whole order
-- subtotal, so every product in every branch earned the same 10%. It now
-- resolves per line item through the same three-level fallback the price and
-- discount already use:
--
--   branch_inventory.cashback_percentage   (Marketing Manager, this branch)
--     -> products.cashback_percentage      (Super Admin, all branches)
--       -> settings.cashback_rate          (platform default, 10%)
--
-- NULL means "not overridden here" — that is what makes the fallback work, so
-- neither column gets a DEFAULT.
--
-- Idempotent: safe to re-run on every deploy.
-- =============================================================================

ALTER TABLE products
    ADD COLUMN IF NOT EXISTS cashback_percentage DOUBLE PRECISION;

COMMENT ON COLUMN products.cashback_percentage IS
    'Global default cashback % (0-100). NULL = use the platform cashback rate.';

ALTER TABLE branch_inventory
    ADD COLUMN IF NOT EXISTS cashback_percentage DOUBLE PRECISION;

COMMENT ON COLUMN branch_inventory.cashback_percentage IS
    'Branch override cashback % (0-100). NULL = fall back to products.cashback_percentage.';
