-- =============================================================================
-- Migration 044: Order fulfilment milestone timestamps (QA spec B1)
--
-- Adds three nullable timestamp columns, mirroring the existing shipped_at /
-- delivered_at pattern. Set by OrderService.update_status on the transition
-- into each state; feed the customer order timeline and the admin audit view.
--
-- The new order STATUSES themselves (packing / packed / handed_to_courier /
-- delivery_failed / rto_initiated) need NO schema change: orders.status is a
-- plain VARCHAR(30), not a Postgres enum. The only guardrails are app-side
-- (app/services/order_workflow.py).
--
-- SQLAlchemy Base.metadata.create_all() also adds these on a fresh DB; this
-- file is for pre-existing databases. Idempotent -- safe to re-run.
-- =============================================================================

ALTER TABLE orders ADD COLUMN IF NOT EXISTS packed_at            TIMESTAMPTZ;
ALTER TABLE orders ADD COLUMN IF NOT EXISTS handed_to_courier_at TIMESTAMPTZ;
ALTER TABLE orders ADD COLUMN IF NOT EXISTS delivery_failed_at   TIMESTAMPTZ;
