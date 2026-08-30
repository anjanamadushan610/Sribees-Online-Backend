-- =============================================================================
-- 045_courier_remittance_and_retry.sql
--
-- SribeesExpress's round-2 response added three things to the shipment
-- payload (docs/SRIBEESEXPRESS_ECOMMERCE_API.md sections 7, 7a, 8):
--
--   * updated_at    -- the field that was filterable but never serialized.
--                      Not stored: it is a sweep cursor input, not order state
--                      (courier_tracking_updated_at already holds what the
--                      order screens need).
--   * will_retry    -- whether a `failed` shipment can still be redispatched.
--                      This is the field that decides refund-now vs
--                      tell-the-customer-tomorrow, so it has to be persisted,
--                      not just read once off a webhook.
--   * remittance_id -- NULL until the COD collected on this shipment has been
--                      paid out to us. This is how an order answers "has our
--                      money actually arrived?" without a date-range scan
--                      across the remittance API.
--
-- Idempotent: safe to re-run on every deploy.
-- =============================================================================

ALTER TABLE orders
    ADD COLUMN IF NOT EXISTS courier_will_retry     BOOLEAN,
    ADD COLUMN IF NOT EXISTS courier_remittance_id  INTEGER;

COMMENT ON COLUMN orders.courier_will_retry IS
    'Only meaningful when courier_tracking_status = failed. TRUE = SribeesExpress can still redispatch; FALSE = it is on its way to returned; NULL = not applicable. There is deliberately no next_attempt_at -- nothing on their side schedules a retry on a clock.';
COMMENT ON COLUMN orders.courier_remittance_id IS
    'SribeesExpress remittance run that paid out this shipment''s collected COD. NULL until settled. Note their remittance runs are triggered by ops, not on a scheduler, so NULL is normal for days.';

-- Finding "COD collected but the money has not reached us" is the whole point
-- of the column, so index exactly that: collected, not yet remitted.
CREATE INDEX IF NOT EXISTS idx_orders_courier_cod_unremitted
    ON orders (courier_cod_collected_at)
    WHERE courier_cod_collected IS TRUE
      AND courier_remittance_id IS NULL;
