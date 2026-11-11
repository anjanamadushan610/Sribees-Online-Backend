-- Staff new-order alerts (2026-11-11): the Sribees New Orders app rings branch
-- staff and super admins when an order becomes workable.
-- Additive and replay-safe: every deploy re-applies every file.

-- One row per staff device. The token is unique across admins: a shared phone
-- that signs in as someone else is re-bound to them, so the previous admin
-- stops getting that phone's alerts.
CREATE TABLE IF NOT EXISTS admin_push_tokens (
    token_id     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    admin_id     UUID NOT NULL REFERENCES admins(admin_id) ON DELETE CASCADE,
    token        VARCHAR(512) NOT NULL UNIQUE,
    device_id    VARCHAR(255) NOT NULL,
    platform     VARCHAR(16),
    app_name     VARCHAR(32) NOT NULL DEFAULT 'new_orders',
    language     VARCHAR(5) NOT NULL DEFAULT 'si',
    is_active    BOOLEAN NOT NULL DEFAULT TRUE,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
-- Same names as the ORM model (app/models/admin_push.py): deploys run
-- create_all first, so whichever creates them, there is one of each.
CREATE INDEX IF NOT EXISTS ix_admin_push_tokens_admin_id ON admin_push_tokens(admin_id);
CREATE INDEX IF NOT EXISTS ix_admin_push_tokens_device_id ON admin_push_tokens(device_id);

-- The send-once latch: an order's alert goes out only by the call that
-- inserts its row here. A retried payment callback, a reconciliation sweep
-- and the checkout itself can all reach the hook; only one of them rings.
CREATE TABLE IF NOT EXISTS admin_order_alerts (
    order_id   UUID PRIMARY KEY REFERENCES orders(order_id) ON DELETE CASCADE,
    sent_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    -- NULL until the send finishes (see the model): claimed-but-unfinished
    -- alerts stay visible.
    devices    INTEGER,
    recipients INTEGER
);
