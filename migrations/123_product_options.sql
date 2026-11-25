-- =============================================================================
-- 123 - Product options: one product in several shades / colours (owner, 2026-11-25)
-- =============================================================================
--
-- products.option_label   "Shade", "Colour" ... NULL = a plain product. Set =
--                         the customer must pick one of its options; the
--                         options are product_variants rows with weight_grams
--                         NULL (generic variants), each with its own photo
--                         (image_url) and its own stock per branch
--                         (branch_variant_inventory, stock_mode 'packed').
-- product_variants.price  NULL now means "the product's own price": the
--                         branch price, discount, Quick Sale and cashback all
--                         apply as to the product. A set price is an override
--                         for that one option. Weight options always have one.
--
-- REPLAYED ON EVERY DEPLOY: idempotent.
-- =============================================================================

BEGIN;

ALTER TABLE products ADD COLUMN IF NOT EXISTS option_label VARCHAR(20);

ALTER TABLE product_variants ALTER COLUMN price DROP NOT NULL;

DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'chk_variant_weight_has_price') THEN
        ALTER TABLE product_variants ADD CONSTRAINT chk_variant_weight_has_price
            CHECK (weight_grams IS NULL OR price IS NOT NULL);
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_product_variants_product ON product_variants (product_id);

COMMIT;
