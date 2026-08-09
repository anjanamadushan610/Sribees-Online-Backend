-- =============================================================================
-- Migration 033: Branch-to-branch stock transfers
--
-- admin/src/pages/Inventory/StockTransfers.tsx and LowStockReport.tsx's
-- "Request Transfer" action have shipped since the early UI pass with a full
-- request/approve/ship/complete workflow — entirely against mock data, no
-- backend behind either. This is the real table.
--
-- Lifecycle: pending -> approved -> in_transit -> completed, or
-- pending -> cancelled (reject). Stock moves twice: `from_branch`'s
-- branch_inventory.stock_quantity is decremented on approval (commits the
-- units so they can't be double-allocated to another transfer or an order),
-- and `to_branch`'s is incremented on completion (arrival).
-- =============================================================================

CREATE TABLE IF NOT EXISTS stock_transfers (
    transfer_id     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    from_branch_id  UUID NOT NULL REFERENCES branches(branch_id) ON DELETE RESTRICT,
    to_branch_id    UUID NOT NULL REFERENCES branches(branch_id) ON DELETE RESTRICT,
    product_id      UUID NOT NULL REFERENCES products(product_id) ON DELETE RESTRICT,
    quantity        INTEGER NOT NULL CHECK (quantity > 0),
    status          VARCHAR(20) NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending', 'approved', 'in_transit', 'completed', 'cancelled')),
    requested_by_id UUID NOT NULL REFERENCES admins(admin_id) ON DELETE RESTRICT,
    approved_by_id  UUID REFERENCES admins(admin_id) ON DELETE SET NULL,
    notes           TEXT,
    requested_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    approved_at     TIMESTAMPTZ,
    shipped_at      TIMESTAMPTZ,
    completed_at    TIMESTAMPTZ,
    CONSTRAINT chk_stock_transfer_branches_differ CHECK (from_branch_id <> to_branch_id)
);

CREATE INDEX IF NOT EXISTS idx_stock_transfers_from_branch ON stock_transfers(from_branch_id);
CREATE INDEX IF NOT EXISTS idx_stock_transfers_to_branch ON stock_transfers(to_branch_id);
CREATE INDEX IF NOT EXISTS idx_stock_transfers_status ON stock_transfers(status);
CREATE INDEX IF NOT EXISTS idx_stock_transfers_requested_at ON stock_transfers(requested_at DESC);