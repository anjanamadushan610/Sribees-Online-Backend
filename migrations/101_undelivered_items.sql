-- =============================================================================
-- 101 - Partial delivery: the items a COD customer did not take
-- =============================================================================
--
-- SribeesExpress reports a partial delivery as `delivered` with the COD it
-- actually collected (orders.courier_cod_collected_amount). When that is less
-- than the order's total, the customer refused some items and the rider brings
-- them back. Once they are back at the shop, staff record which items (and how
-- many) here: the stock goes back on the shelf and Sribees Finance takes the
-- goods back and the sale off. Written once.
--
-- REPLAYED ON EVERY DEPLOY: idempotent.
-- =============================================================================

BEGIN;

ALTER TABLE orders ADD COLUMN IF NOT EXISTS undelivered_items JSONB;
ALTER TABLE orders ADD COLUMN IF NOT EXISTS undelivered_recorded_at TIMESTAMPTZ;

COMMIT;
