-- Safe to replay; no behavior enabled until DELIVERY_CARD_PAYMENT_ENABLED=true.
ALTER TABLE orders ADD COLUMN IF NOT EXISTS delivery_card_state VARCHAR(20);
ALTER TABLE orders ADD COLUMN IF NOT EXISTS delivery_card_reference VARCHAR(80);
ALTER TABLE payment_attempts ADD COLUMN IF NOT EXISTS purpose VARCHAR(20) NOT NULL DEFAULT 'checkout';
ALTER TABLE payment_attempts ADD COLUMN IF NOT EXISTS checkout_url VARCHAR(2048);
ALTER TABLE payment_attempts ADD COLUMN IF NOT EXISTS courier_synced VARCHAR(10);
CREATE INDEX IF NOT EXISTS ix_delivery_card_reconcile ON payment_attempts (created_at)
 WHERE purpose = 'delivery_card' AND courier_synced IS DISTINCT FROM 'yes';
