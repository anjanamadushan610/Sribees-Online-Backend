-- =============================================================================
-- Migration 047: Return claim resolution (QA spec B4, Module C)
--
-- Records how a Branch Manager / Super Admin settled a return:
--   returnless_refund  -- spoilage, refund only, no physical pickup
--   reverse_pickup     -- refund + a rider is dispatched to collect the goods
--
-- SQLAlchemy create_all() adds these on a fresh DB. Idempotent -- safe to re-run.
-- =============================================================================

ALTER TABLE orders ADD COLUMN IF NOT EXISTS return_resolution      VARCHAR(30);
ALTER TABLE orders ADD COLUMN IF NOT EXISTS return_resolution_note TEXT;
