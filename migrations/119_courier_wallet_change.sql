BEGIN;
CREATE TABLE IF NOT EXISTS courier_wallet_credits (
    source VARCHAR(255) PRIMARY KEY,
    order_id UUID NOT NULL UNIQUE REFERENCES orders(order_id),
    transaction_id UUID NOT NULL UNIQUE REFERENCES wallet_transactions(transaction_id),
    amount NUMERIC(12,2) NOT NULL CHECK (amount > 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
COMMIT;
