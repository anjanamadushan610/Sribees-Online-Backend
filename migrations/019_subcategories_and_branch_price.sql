-- Migration 019: Sub-categories + branch price overrides
--
-- Completes the Global-Catalog → Branch-Override architecture:
--
--   1. products.subcategory_id — the leaf-level assignment. The column exists
--      in the SQLAlchemy model but was never migrated, so any database created
--      before it was added to models/product.py is missing it entirely
--      (Base.metadata.create_all() creates tables but never ALTERs them).
--
--   2. branch_inventory price/discount override columns — the merge logic in
--      ProductService already COALESCEs these down to the global product, but
--      they may not exist on older databases.
--
-- Safe to re-run: every statement uses IF NOT EXISTS.

-- ---------------------------------------------------------------------------
-- 1. Sub-category assignment on the global product
-- ---------------------------------------------------------------------------

ALTER TABLE products
    ADD COLUMN IF NOT EXISTS subcategory_id UUID;

-- FK added separately: ADD CONSTRAINT has no IF NOT EXISTS, so guard on catalog.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'fk_products_subcategory'
    ) THEN
        ALTER TABLE products
            ADD CONSTRAINT fk_products_subcategory
            FOREIGN KEY (subcategory_id)
            REFERENCES categories (category_id)
            ON DELETE SET NULL;
    END IF;
END
$$;

CREATE INDEX IF NOT EXISTS idx_products_subcategory
    ON products (subcategory_id);

-- Listing a category page filters on "category OR its sub-categories".
CREATE INDEX IF NOT EXISTS idx_products_category_subcategory
    ON products (category_id, subcategory_id);

-- ---------------------------------------------------------------------------
-- 2. Branch-level overrides (NULL = fall back to the global product value)
-- ---------------------------------------------------------------------------

ALTER TABLE branch_inventory
    ADD COLUMN IF NOT EXISTS branch_price        NUMERIC(10, 2),
    ADD COLUMN IF NOT EXISTS discount_percentage DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS is_on_sale          BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS is_active           BOOLEAN NOT NULL DEFAULT TRUE;

-- A branch may only carry a given product once — this is what makes the
-- override upsert (ON CONFLICT) in ProductService.upsert_branch_override safe.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'uq_branch_inventory_product_branch'
    ) THEN
        ALTER TABLE branch_inventory
            ADD CONSTRAINT uq_branch_inventory_product_branch
            UNIQUE (product_id, branch_id);
    END IF;
END
$$;

-- Guard rails: a negative or absurd override is a data-entry error, not a sale.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'ck_branch_inventory_price_positive'
    ) THEN
        ALTER TABLE branch_inventory
            ADD CONSTRAINT ck_branch_inventory_price_positive
            CHECK (branch_price IS NULL OR branch_price >= 0);
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'ck_branch_inventory_discount_range'
    ) THEN
        ALTER TABLE branch_inventory
            ADD CONSTRAINT ck_branch_inventory_discount_range
            CHECK (
                discount_percentage IS NULL
                OR (discount_percentage >= 0 AND discount_percentage <= 100)
            );
    END IF;
END
$$;

-- The customer-facing branch listing always filters on (branch, active, stock).
CREATE INDEX IF NOT EXISTS idx_branch_inv_visibility
    ON branch_inventory (branch_id, is_active, stock_quantity);
