-- The most a branch wants to hold of a product (owner, 2026-11-26). Sribees Supply
-- prepares a draft order when the stock falls below low_stock_threshold, up to this.
-- NULL = not reordered automatically. Safe to replay.
ALTER TABLE branch_inventory ADD COLUMN IF NOT EXISTS max_stock_level INTEGER;
DO $$ BEGIN
    ALTER TABLE branch_inventory ADD CONSTRAINT chk_branch_inventory_max_stock
        CHECK (max_stock_level IS NULL OR max_stock_level > 0);
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
