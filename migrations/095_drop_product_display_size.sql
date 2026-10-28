-- The owner withdrew the separate customer display size (2026-10-28): a
-- product's size lives in its name and weight. 093 added these columns on
-- production before it was reverted, so drop them where they exist.
ALTER TABLE products DROP COLUMN IF EXISTS display_size;
ALTER TABLE products DROP COLUMN IF EXISTS display_size_unit;
ALTER TABLE order_items DROP COLUMN IF EXISTS display_size;
ALTER TABLE order_items DROP COLUMN IF EXISTS display_size_unit;
