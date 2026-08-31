-- =============================================================================
-- Migration 047: Order → branch-inventory stock bookkeeping
--
-- Wires the long-dormant branch_inventory.reserved_quantity column into the
-- order lifecycle (app/services/inventory_service.py):
--
--   * checkout            → reserved_quantity += line qty   (stock_reserved_at)
--   * handed_to_courier    → stock_quantity   -= line qty
--                            reserved_quantity -= line qty   (stock_committed_at)
--   * cancel (pre-dispatch) → reserved_quantity -= line qty
--   * cancel (post-dispatch, Super Admin override) → stock_quantity += line qty
--
-- Both new columns are NULL-guarded idempotency markers.
--
-- SQLAlchemy create_all() adds these on a fresh DB; this file is for
-- pre-existing databases. Idempotent — safe to re-run.
-- =============================================================================

ALTER TABLE orders ADD COLUMN IF NOT EXISTS stock_reserved_at  TIMESTAMPTZ;
ALTER TABLE orders ADD COLUMN IF NOT EXISTS stock_committed_at TIMESTAMPTZ;

-- Backfill: orders already delivered/shipped before this shipped have had their
-- stock consumed in the real world but never recorded here. Mark them committed
-- so a later status re-trigger can't double-deduct. Reserved is left untouched
-- (there is nothing reliable to reconstruct, and reserved only ever decrements
-- toward zero via GREATEST(...,0)).
UPDATE orders
   SET stock_committed_at = COALESCE(shipped_at, delivered_at, updated_at, now())
 WHERE stock_committed_at IS NULL
   AND status IN ('shipped', 'out_for_delivery', 'delivered',
                  'return_requested', 'return_approved', 'refunded');
