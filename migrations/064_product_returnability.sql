-- =============================================================================
-- 064_product_returnability.sql — Some products can't be returned.
--
-- The super admin decides per product (a toggle on the product form):
-- perishables, opened personal care and the like are sold final. The flag is
-- copied onto every order item at checkout, the way unit_price is, so the
-- terms a customer bought under never change after the fact; the return
-- request checks the item's copy, not the product.
--
-- Everything that exists today stays returnable, which is what it was.
-- Idempotent: safe to re-run on every deploy.
-- =============================================================================

ALTER TABLE products
    ADD COLUMN IF NOT EXISTS is_returnable BOOLEAN NOT NULL DEFAULT TRUE;

COMMENT ON COLUMN products.is_returnable IS
    'Whether customers may return this product after delivery. Copied to order_items.is_returnable at checkout.';

ALTER TABLE order_items
    ADD COLUMN IF NOT EXISTS is_returnable BOOLEAN NOT NULL DEFAULT TRUE;

COMMENT ON COLUMN order_items.is_returnable IS
    'Snapshot of products.is_returnable when the order was placed; the return request checks this.';
