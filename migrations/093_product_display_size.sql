-- Customer-facing size only. Do not copy the courier weight into these fields.
ALTER TABLE products ADD COLUMN IF NOT EXISTS display_size VARCHAR(80);
ALTER TABLE products ADD COLUMN IF NOT EXISTS display_size_unit VARCHAR(32);
ALTER TABLE order_items ADD COLUMN IF NOT EXISTS display_size VARCHAR(80);
ALTER TABLE order_items ADD COLUMN IF NOT EXISTS display_size_unit VARCHAR(32);
