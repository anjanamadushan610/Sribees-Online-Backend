-- =============================================================================
-- Migration 057: a record of every cancellation, and who made it.
--
-- Cancelling is about to become a button in the customer's app rather than a
-- support conversation, and a button that moves money is a button people will
-- test. This table is what makes the pattern visible: one row per cancelled
-- order, carrying where the request came from, how quickly it arrived after
-- the order was placed, and what it cost us.
--
-- A separate table rather than more columns on `orders` because these rows are
-- read together, by one screen, for a purpose the orders table does not serve
-- -- and because an order is cancelled at most once, so nothing here needs to
-- live on the hot path of every order query.
--
-- The IP is kept because "the same address cancelling thirty orders" is the
-- single clearest signal of automation, and it cannot be reconstructed after
-- the fact. It is personal data: it exists to spot abuse, not to profile
-- customers, and it should be dropped on the same schedule as the rest of the
-- order record.
--
-- SQLAlchemy create_all() adds this on a fresh DB. Idempotent -- the deploy
-- applies this file again on each release.
-- =============================================================================

CREATE TABLE IF NOT EXISTS order_cancellations (
    cancellation_id  UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    -- One cancellation per order. The UNIQUE constraint is the guard that
    -- makes a double-submit -- a customer tapping twice on a slow connection,
    -- a retried request -- impossible to record as two events, and therefore
    -- impossible to refund twice through this path.
    order_id         UUID NOT NULL UNIQUE REFERENCES orders(order_id) ON DELETE CASCADE,
    user_id          UUID REFERENCES users(user_id) ON DELETE SET NULL,

    -- 'customer' or 'admin'. An admin cancelling on someone's behalf is a
    -- normal operation and must not be counted against the customer.
    cancelled_by     VARCHAR(20) NOT NULL DEFAULT 'customer',
    actor_admin_id   UUID,

    reason           TEXT,

    -- Where it came from. All nullable: a cancellation raised through the
    -- admin console has no device, and a proxy may strip the client address.
    ip_address       VARCHAR(64),
    user_agent       TEXT,
    device_id        VARCHAR(128),
    client_platform  VARCHAR(40),

    -- Both timestamps, denormalised on purpose: the elapsed time is the
    -- interesting figure and it must stay correct even if the order row is
    -- later touched.
    order_placed_at  TIMESTAMPTZ,
    cancelled_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- What it cost. refund_amount is what was credited to the wallet; 0 for a
    -- COD order, where nothing had been paid.
    refund_amount    NUMERIC(10, 2) NOT NULL DEFAULT 0,
    refund_destination VARCHAR(20) NOT NULL DEFAULT 'none',

    -- The delivery charge kept back when the parcel had already been booked
    -- with SribeesExpress. Recorded separately from refund_amount so the pair
    -- reads as "they paid X, we kept Y for the courier" rather than one net
    -- figure nobody can reconcile against the order.
    courier_fee      NUMERIC(10, 2) NOT NULL DEFAULT 0,

    -- Set by the service when the pattern is worth a human look. Never blocks
    -- the cancellation or the refund: the customer is not made to wait while
    -- somebody decides whether they are a fraud.
    flagged          BOOLEAN NOT NULL DEFAULT FALSE,
    flag_reason      TEXT,

    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- The analysis screen's three questions: who cancels most, what is happening
-- right now, and which address is behind it.
CREATE INDEX IF NOT EXISTS ix_order_cancellations_user
    ON order_cancellations (user_id, cancelled_at DESC);

CREATE INDEX IF NOT EXISTS ix_order_cancellations_recent
    ON order_cancellations (cancelled_at DESC);

CREATE INDEX IF NOT EXISTS ix_order_cancellations_ip
    ON order_cancellations (ip_address)
    WHERE ip_address IS NOT NULL;

-- Partial: the flagged rows are the small set anyone opens first, and a
-- partial index stays tiny however many cancellations accumulate.
CREATE INDEX IF NOT EXISTS ix_order_cancellations_flagged
    ON order_cancellations (cancelled_at DESC)
    WHERE flagged;

-- Added after the table first shipped to development. Separate statement so a
-- database that already has the table picks the column up on the next deploy.
ALTER TABLE order_cancellations
    ADD COLUMN IF NOT EXISTS courier_fee NUMERIC(10, 2) NOT NULL DEFAULT 0;
