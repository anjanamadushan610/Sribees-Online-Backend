-- Ring until accepted (owner, 2026-11-11): an order nobody has accepted rings
-- the staff phones again every 2 minutes for 30 minutes; at night (22:00-07:00
-- Colombo) once on arrival and again from 07:00. Additive and replay-safe.
--
-- Existing rows (alerts sent before this) keep next_ring_at NULL, so no old
-- order starts ringing on deploy.
ALTER TABLE admin_order_alerts ADD COLUMN IF NOT EXISTS alert_status VARCHAR(30);
ALTER TABLE admin_order_alerts ADD COLUMN IF NOT EXISTS rings INTEGER NOT NULL DEFAULT 0;
ALTER TABLE admin_order_alerts ADD COLUMN IF NOT EXISTS window_started_at TIMESTAMPTZ;
ALTER TABLE admin_order_alerts ADD COLUMN IF NOT EXISTS next_ring_at TIMESTAMPTZ;
ALTER TABLE admin_order_alerts ADD COLUMN IF NOT EXISTS stopped_at TIMESTAMPTZ;
-- Phones told the order was accepted (clear push). Separate from stopped_at:
-- an order can stop ringing (window over) and be accepted much later.
ALTER TABLE admin_order_alerts ADD COLUMN IF NOT EXISTS cleared_at TIMESTAMPTZ;

-- The sweep reads only rows still due to ring.
CREATE INDEX IF NOT EXISTS ix_admin_order_alerts_next_ring_at
    ON admin_order_alerts (next_ring_at) WHERE stopped_at IS NULL;

-- The clear pass reads only rows whose phones were not told yet.
CREATE INDEX IF NOT EXISTS ix_admin_order_alerts_uncleared
    ON admin_order_alerts (order_id) WHERE cleared_at IS NULL AND alert_status IS NOT NULL;
