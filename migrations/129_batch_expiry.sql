-- Expiry for stock batches (owner plan 2026-11-27, Sribees Supply barcode-first receiving).
-- * expiry_alert_days: how many days before expiry a batch shows as "close to expiry".
--   Set on a category (a sub-category wins over its parent) and, when one product needs
--   its own, on the product. Nothing set = 30 days (in code).
-- * A batch whose expiry date has passed is not sold. The expiry sweep takes its remaining
--   units off the branch's stock and records how many in expired_units / expired_at.
-- Safe to replay.
ALTER TABLE categories ADD COLUMN IF NOT EXISTS expiry_alert_days SMALLINT
    CHECK (expiry_alert_days BETWEEN 1 AND 365);
ALTER TABLE products ADD COLUMN IF NOT EXISTS expiry_alert_days SMALLINT
    CHECK (expiry_alert_days BETWEEN 1 AND 365);
ALTER TABLE inventory_batches ADD COLUMN IF NOT EXISTS expired_at TIMESTAMPTZ;
ALTER TABLE inventory_batches ADD COLUMN IF NOT EXISTS expired_units INTEGER
    CHECK (expired_units >= 0);
CREATE INDEX IF NOT EXISTS ix_inventory_batches_expiring ON inventory_batches (expires_on)
    WHERE remaining > 0 AND expires_on IS NOT NULL;
