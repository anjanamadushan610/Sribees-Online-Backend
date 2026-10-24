-- =============================================================================
-- 086 - How often each product's price is re-checked
-- =============================================================================
--
-- The Sribees Price app (price-change app for managers) groups the catalogue
-- into four tabs by how often the shelf price moves:
--
--   daily     vegetables, fruit, fish, meat -- market prices
--   weekly    rice, sugar, dhal, oil -- staples
--   monthly   packaged goods whose MRP changes now and then
--   long      barcoded packaged goods; prices rarely move
--
-- The super admin picks the level when adding a product; NULL means nobody
-- has chosen one yet (the admin list filters them with ?price_review_level=unset).
--
-- REPLAYED ON EVERY DEPLOY. The backfill runs only in the deploy that adds the
-- column, so a level an admin later changes is never put back by a replay.
-- Owner's rule (2026-10-24): every product that already has a barcode starts
-- as "long". The POS import stored the barcode in sku (EAN-8 / UPC / EAN-13,
-- a few with the scanner's "]C1" symbology prefix); products without one got
-- sku "POS-<id>" or an admin-suggested "CAT-NAME-001" and stay NULL.
-- =============================================================================

BEGIN;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
         WHERE table_name = 'products' AND column_name = 'price_review_level'
    ) THEN
        ALTER TABLE products ADD COLUMN price_review_level VARCHAR(10);

        UPDATE products
           SET price_review_level = 'long'
         WHERE sku ~ '^[0-9]{8,14}$'
            OR sku ~ '^\][A-Za-z][0-9]{8,}$';
    END IF;
END $$;

ALTER TABLE products DROP CONSTRAINT IF EXISTS ck_products_price_review_level;
ALTER TABLE products ADD CONSTRAINT ck_products_price_review_level
    CHECK (price_review_level IS NULL
           OR price_review_level IN ('daily', 'weekly', 'monthly', 'long'));

CREATE INDEX IF NOT EXISTS ix_products_price_review_level
    ON products (price_review_level);

COMMENT ON COLUMN products.price_review_level IS
    'How often the price is re-checked: daily, weekly, monthly or long. Groups the Sribees Price app''s tabs; NULL = not chosen yet.';

COMMIT;
