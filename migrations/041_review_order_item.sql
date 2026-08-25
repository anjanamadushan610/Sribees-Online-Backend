-- =============================================================================
-- Migration 041: link reviews to the delivered order item that justifies them
--
-- Reviews previously had no relationship to a purchase at all — any logged-in
-- customer could review any product, any time. New reviews must now name the
-- order_item they're reviewing, validated server-side (app/services/
-- product_review_service.py) to belong to the reviewer, match the product,
-- and come from an order whose status is 'delivered'.
--
-- order_item_id is nullable and SET NULL on delete: existing reviews written
-- before this column existed keep their current is_verified_purchase value
-- and stay visible — this migration does not touch or invalidate them.
--
-- Schema is also created by SQLAlchemy Base.metadata.create_all() on fresh
-- DBs; run this against pre-existing databases. Safe to re-run.
-- =============================================================================

ALTER TABLE reviews ADD COLUMN IF NOT EXISTS order_item_id UUID
    REFERENCES order_items(order_item_id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS idx_reviews_order_item_id ON reviews (order_item_id);
