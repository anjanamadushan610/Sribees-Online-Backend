-- =============================================================================
-- 087 - Every product has a price review level; "long" by default
-- =============================================================================
--
-- Owner's rule (2026-10-24): no product may be without a level. The admin
-- form's dropdown starts on "long", and the products 086 left NULL (no
-- barcode SKU) become "long" too.
--
-- Idempotent: safe to re-run on every deploy. Once the column is NOT NULL the
-- UPDATE finds nothing, so a level an admin chose is never overwritten.
-- =============================================================================

BEGIN;

UPDATE products SET price_review_level = 'long' WHERE price_review_level IS NULL;

ALTER TABLE products ALTER COLUMN price_review_level SET DEFAULT 'long';
ALTER TABLE products ALTER COLUMN price_review_level SET NOT NULL;

COMMENT ON COLUMN products.price_review_level IS
    'How often the price is re-checked: daily, weekly, monthly or long (default). Groups the Sribees Price app''s tabs.';

COMMIT;
