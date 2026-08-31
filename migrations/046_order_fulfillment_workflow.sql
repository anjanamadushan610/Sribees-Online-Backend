-- =============================================================================
-- Migration 046: Branch order-fulfilment workflow milestones
--
-- Adds two nullable timestamp columns mirroring the existing shipped_at /
-- delivered_at pattern. They are stamped by OrderService.update_status on the
-- transition into the matching state and feed both the customer order timeline
-- (app/api/v1/orders.py) and the admin audit view.
--
-- The new order statuses themselves (packing / packed / handed_to_courier)
-- need NO schema change: orders.status is a plain VARCHAR(30), not a Postgres
-- enum. The only guardrails are app-side (app/services/order_workflow.py).
--
-- SQLAlchemy Base.metadata.create_all() also adds these columns on a fresh DB;
-- this file is for pre-existing databases. Idempotent — safe to re-run.
-- =============================================================================

ALTER TABLE orders ADD COLUMN IF NOT EXISTS packed_at            TIMESTAMPTZ;
ALTER TABLE orders ADD COLUMN IF NOT EXISTS handed_to_courier_at TIMESTAMPTZ;
