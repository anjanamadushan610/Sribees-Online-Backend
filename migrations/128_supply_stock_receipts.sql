-- Goods received in Sribees Supply that were added to a branch's stock
-- (POST /internal/supply/receipts). One row per Supply receipt line, so a line that is
-- sent again is never added twice. Safe to replay.
CREATE TABLE IF NOT EXISTS supply_stock_receipts (
    line_id UUID PRIMARY KEY,
    receipt_no VARCHAR(40) NOT NULL,
    branch_id UUID NOT NULL,
    product_id UUID NOT NULL,
    variant_id UUID,
    units INTEGER NOT NULL CHECK (units > 0),
    grams INTEGER CHECK (grams > 0),
    purchase_cost NUMERIC(10,2) CHECK (purchase_cost >= 0),
    batch_id UUID,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_supply_stock_receipts_product_id ON supply_stock_receipts (product_id);
