-- =============================================================================
-- 132 - A branch's fulfilment partner is working (Sribees Supply, owner 2026-11-27)
-- =============================================================================
--
-- branches.fulfilment_partner_active: Sribees Supply sets it when a partner shop (first:
-- Nimali Stores for Matugama) fills the branch's "Other Products" (branch_inventory.
-- sourcing_group external_daily / external_slow). While it is on, the partner sets those
-- products' branch price and stock through Supply, and a branch-scoped admin cannot change
-- their price (Super Admin still can). Off (the default) = everything as before.
--
-- REPLAYED ON EVERY DEPLOY: idempotent.
-- =============================================================================

ALTER TABLE branches ADD COLUMN IF NOT EXISTS fulfilment_partner_active BOOLEAN NOT NULL DEFAULT FALSE;
