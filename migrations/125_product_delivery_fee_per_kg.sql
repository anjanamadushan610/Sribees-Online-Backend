ALTER TABLE products ADD COLUMN IF NOT EXISTS delivery_fee_per_kg NUMERIC(10, 2)
    CHECK (delivery_fee_per_kg > 0);
