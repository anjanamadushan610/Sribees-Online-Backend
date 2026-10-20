-- =============================================================================
-- Migration 080: a product can also sit on a second shelf.
--
-- products.category_id / subcategory_id remain the product's one home. A row
-- here lists the product under one more sub-category as well (and so under
-- that sub-category's parent) -- e.g. an 18g milk powder sachet filed under
-- Dairy > Milk Powder that also belongs on Beverages > Tea. The customer app
-- already asks for products by category_id / subcategory_id; the server now
-- answers with both homes, so no app change is needed.
--
-- Idempotent -- the deploy applies this file again on each release.
-- =============================================================================

CREATE TABLE IF NOT EXISTS product_extra_categories (
    product_id      UUID NOT NULL REFERENCES products(product_id) ON DELETE CASCADE,
    subcategory_id  UUID NOT NULL REFERENCES categories(category_id) ON DELETE CASCADE,
    created_at      TIMESTAMPTZ DEFAULT now(),
    PRIMARY KEY (product_id, subcategory_id)
);

CREATE INDEX IF NOT EXISTS idx_product_extra_categories_subcategory
    ON product_extra_categories (subcategory_id);
