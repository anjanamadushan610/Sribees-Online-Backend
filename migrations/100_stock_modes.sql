-- =============================================================================
-- 100 - How each product keeps stock, and Sribees Finance's stock per item
-- =============================================================================
--
-- products.stock_mode (owner, 2026-11-03: the store has both kinds):
--   unit    normal goods; branch_inventory.stock_quantity counts units (as before)
--   loose   weighed into the bag per order; branch_inventory.stock_grams
--           holds the grams and a weight option takes quantity x weight_grams
--   packed  bags of each size on the shelf; each weight option has its own
--           count per branch in branch_variant_inventory
-- Every product starts as 'unit', so nothing changes until an admin chooses.
--
-- finance_stock_levels gets one row per stock item: a product, or a pack size
-- of a pre-packed product (item_id = the variant).
--
-- REPLAYED ON EVERY DEPLOY: idempotent.
-- =============================================================================

BEGIN;

ALTER TABLE products ADD COLUMN IF NOT EXISTS stock_mode VARCHAR(8) NOT NULL DEFAULT 'unit';
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'chk_products_stock_mode') THEN
        ALTER TABLE products ADD CONSTRAINT chk_products_stock_mode
            CHECK (stock_mode IN ('unit', 'loose', 'packed'));
    END IF;
END $$;

-- A loose product's stock in grams. branch_inventory.stock_quantity stays the figure
-- every screen and filter reads: packs of the smallest weight option for a loose
-- product, all packs together for a pre-packed one (kept by app/services/stock_keeping.py).
ALTER TABLE branch_inventory ADD COLUMN IF NOT EXISTS stock_grams INTEGER NOT NULL DEFAULT 0;

CREATE TABLE IF NOT EXISTS branch_variant_inventory (
    branch_id      UUID        NOT NULL REFERENCES branches(branch_id) ON DELETE CASCADE,
    variant_id     UUID        NOT NULL REFERENCES product_variants(variant_id) ON DELETE CASCADE,
    stock_quantity INTEGER     NOT NULL DEFAULT 0 CHECK (stock_quantity >= 0),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (branch_id, variant_id)
);

ALTER TABLE finance_stock_levels ADD COLUMN IF NOT EXISTS variant_id UUID;
ALTER TABLE finance_stock_levels ADD COLUMN IF NOT EXISTS item_id UUID;
UPDATE finance_stock_levels SET item_id = COALESCE(variant_id, product_id) WHERE item_id IS NULL;
ALTER TABLE finance_stock_levels ALTER COLUMN item_id SET NOT NULL;
DO $$ BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_index i
        JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY (i.indkey)
        WHERE i.indrelid = 'finance_stock_levels'::regclass AND i.indisprimary AND a.attname = 'item_id'
    ) THEN
        ALTER TABLE finance_stock_levels DROP CONSTRAINT finance_stock_levels_pkey;
        ALTER TABLE finance_stock_levels ADD PRIMARY KEY (branch_id, item_id);
    END IF;
END $$;

COMMIT;
