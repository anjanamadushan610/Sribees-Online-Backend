-- =============================================================================
-- 088 - Products sold by weight, with a price for each weight option
-- =============================================================================
--
-- Loose goods (sugar, rice, dhal, onions) are bought by weight: the customer
-- picks 250 g ... 5 kg on the product page and each option has its own price.
--
--   products.sold_by_weight        the switch the super admin turns on
--   product_variants.weight_grams  a weight option (the variant's price is the
--                                  global price of that pack)
--   branch_variant_prices          one branch's own price for an option
--   order_items.variant_name       the option bought ("500g"), a snapshot
--
-- REPLAYED ON EVERY DEPLOY. The backfill runs only in the deploy that adds
-- products.sold_by_weight. Owner's rule (2026-10-25): the loose staples added
-- on 2026-10-24 that are sold by the kilo (SKU SB-*, weight 1 kg) turn on, with
-- the default options 250 g, 500 g, 1 kg, 2 kg and 5 kg priced pro rata from
-- the product's 1 kg price. Coconut and egg (no weight) stay off.
-- =============================================================================

BEGIN;

ALTER TABLE product_variants ADD COLUMN IF NOT EXISTS weight_grams INTEGER;
ALTER TABLE product_variants DROP CONSTRAINT IF EXISTS ck_product_variants_weight_grams;
ALTER TABLE product_variants ADD CONSTRAINT ck_product_variants_weight_grams
    CHECK (weight_grams IS NULL OR weight_grams > 0);
CREATE UNIQUE INDEX IF NOT EXISTS uq_product_variants_weight
    ON product_variants (product_id, weight_grams) WHERE weight_grams IS NOT NULL;

CREATE TABLE IF NOT EXISTS branch_variant_prices (
    id          UUID PRIMARY KEY,
    branch_id   UUID NOT NULL REFERENCES branches(branch_id) ON DELETE CASCADE,
    variant_id  UUID NOT NULL REFERENCES product_variants(variant_id) ON DELETE CASCADE,
    price       NUMERIC(10, 2) NOT NULL CHECK (price >= 0),
    created_at  TIMESTAMPTZ DEFAULT now(),
    updated_at  TIMESTAMPTZ DEFAULT now(),
    CONSTRAINT uq_branch_variant_price UNIQUE (branch_id, variant_id)
);
CREATE INDEX IF NOT EXISTS ix_branch_variant_prices_variant_id
    ON branch_variant_prices (variant_id);

ALTER TABLE order_items ADD COLUMN IF NOT EXISTS variant_name VARCHAR(50);

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
         WHERE table_name = 'products' AND column_name = 'sold_by_weight'
    ) THEN
        ALTER TABLE products
            ADD COLUMN sold_by_weight BOOLEAN NOT NULL DEFAULT FALSE;

        UPDATE products
           SET sold_by_weight = TRUE
         WHERE sku LIKE 'SB-%' AND weight = 1 AND weight_unit = 'kg';

        INSERT INTO product_variants
               (variant_id, product_id, name, price, stock_quantity, is_active,
                sort_order, weight_grams)
        SELECT gen_random_uuid(), p.product_id, o.label,
               round(p.price * o.grams / 1000.0, 2), 0, TRUE, o.ord, o.grams
          FROM products p
         CROSS JOIN (VALUES (250, '250g', 1), (500, '500g', 2), (1000, '1kg', 3),
                            (2000, '2kg', 4), (5000, '5kg', 5)) AS o(grams, label, ord)
         WHERE p.sold_by_weight
        ON CONFLICT DO NOTHING;
    END IF;
END $$;

COMMENT ON COLUMN products.sold_by_weight IS
    'Bought by weight: the customer picks one of its weight options (product_variants.weight_grams).';
COMMENT ON COLUMN order_items.variant_name IS
    'The weight option bought ("500g") for a sold-by-weight product.';

COMMIT;
