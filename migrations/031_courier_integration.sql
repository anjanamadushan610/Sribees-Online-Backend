-- =============================================================================
-- 031_courier_integration.sql
--
-- Courier quote/shipment/tracking state on orders. See docs/COURIER_API.md for
-- the full contract this backs (weight-based rate quoting, shipment booking,
-- tracking). shipping_amount (existing column) keeps storing the amount
-- actually charged for delivery; courier_quote_id is what lets booking
-- reference the exact quote that produced that number.
--
-- Idempotent: safe to re-run on every deploy.
-- =============================================================================

ALTER TABLE orders
    ADD COLUMN IF NOT EXISTS courier_quote_id            VARCHAR(64),
    ADD COLUMN IF NOT EXISTS courier_quote_expires_at     TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS courier_shipment_id          VARCHAR(64),
    ADD COLUMN IF NOT EXISTS courier_waybill_number       VARCHAR(64),
    ADD COLUMN IF NOT EXISTS courier_tracking_status      VARCHAR(20),
    ADD COLUMN IF NOT EXISTS courier_tracking_updated_at  TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS courier_booking_status       VARCHAR(20) DEFAULT 'pending';

COMMENT ON COLUMN orders.courier_tracking_status IS
    'Fixed vocabulary per COURIER_API.md section 10: booked | picked_up | in_transit | out_for_delivery | delivered | failed | returned | cancelled';
COMMENT ON COLUMN orders.courier_booking_status IS
    'pending (not booked yet) | booked | failed (courier unreachable/rejected -- see OrderService._book_courier_shipment)';

CREATE INDEX IF NOT EXISTS idx_orders_courier_shipment_id
    ON orders (courier_shipment_id) WHERE courier_shipment_id IS NOT NULL;
