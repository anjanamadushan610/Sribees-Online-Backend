-- =============================================================================
-- 065_order_item_cashback_percentage.sql — Snapshot the cashback rate per line.
--
-- Cashback is resolved per product at checkout (branch override -> product
-- default -> platform rate) and only ever stored as an order-level total
-- (orders.cashback_earned). That total is fine for the wallet ledger, but it
-- cannot answer "how many points did THIS item earn" once the order is
-- history and a later rate change would otherwise reshuffle the answer.
--
-- Frozen onto order_items the same way unit_price and is_returnable already
-- are, so a past order always reports the terms a customer actually bought
-- under, not today's rate.
--
-- Idempotent: safe to re-run on every deploy.
-- =============================================================================

ALTER TABLE order_items
    ADD COLUMN IF NOT EXISTS cashback_percentage NUMERIC(5, 2);

COMMENT ON COLUMN order_items.cashback_percentage IS
    'Snapshot of the effective cashback % resolved at checkout for this line (branch override -> product default -> platform rate). Null for orders placed before this column existed.';
