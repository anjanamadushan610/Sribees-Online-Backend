-- =============================================================================
-- Migration 054: post-delivery returns — the 12-hour window, and the reverse
-- pickup that carries the goods back to the branch.
--
-- Until now a return was a claim and a wallet credit: the customer asked, a
-- Branch Manager approved, the money went out, and nothing ever checked that
-- the goods came back. These columns are the other half — the SribeesExpress
-- return leg (its own SXR waybill), the code our counter staff read to the
-- rider, and the milestones the refund now waits on.
--
-- See SribeesExpress/backend-fastapi/docs/RETURNS_ARCHITECTURE.md §7.
--
-- SQLAlchemy create_all() adds these on a fresh DB. Idempotent -- safe to re-run.
-- =============================================================================

-- The reverse leg booked with SribeesExpress (POST /ecommerce/returns).
ALTER TABLE orders ADD COLUMN IF NOT EXISTS return_waybill_number     VARCHAR(64);
ALTER TABLE orders ADD COLUMN IF NOT EXISTS return_booking_status     VARCHAR(20) DEFAULT 'not_required';
ALTER TABLE orders ADD COLUMN IF NOT EXISTS return_booking_error      TEXT;
ALTER TABLE orders ADD COLUMN IF NOT EXISTS return_fee                NUMERIC(10, 2);

-- The handover code our counter staff read to the rider when the goods come
-- back. Stored in plain text here on purpose, and *only* here: SribeesExpress
-- holds a hash and cannot tell us what it was, so if this row does not have
-- it, nobody does and the branch has to ask for a new one. Same posture as
-- `delivery_verification_code`, which we already store for the customer's own
-- handover -- we are the merchant in this exchange, and this is our secret to
-- keep, not a customer credential.
ALTER TABLE orders ADD COLUMN IF NOT EXISTS return_handover_code      VARCHAR(6);

-- Where the leg has got to, mirrored from the courier the same way
-- `courier_tracking_status` is for the forward journey. Kept separate rather
-- than overwritten onto those columns: the forward shipment's own history is
-- what proves the delivery happened, and a return must not erase it.
ALTER TABLE orders ADD COLUMN IF NOT EXISTS return_tracking_status    VARCHAR(20);
ALTER TABLE orders ADD COLUMN IF NOT EXISTS return_tracking_updated_at TIMESTAMPTZ;

-- Milestones. `return_received_at` is the one that matters most: it is when
-- the goods are physically back with us, and it is what the refund now waits
-- for.
ALTER TABLE orders ADD COLUMN IF NOT EXISTS return_approved_at        TIMESTAMPTZ;
ALTER TABLE orders ADD COLUMN IF NOT EXISTS return_collected_at       TIMESTAMPTZ;
ALTER TABLE orders ADD COLUMN IF NOT EXISTS return_received_at        TIMESTAMPTZ;
ALTER TABLE orders ADD COLUMN IF NOT EXISTS refunded_at               TIMESTAMPTZ;

-- The return leg arrives by webhook under its own waybill, so every push has
-- to find its order by this column. Partial: only a handful of orders ever
-- have one.
CREATE INDEX IF NOT EXISTS ix_orders_return_waybill_number
    ON orders (return_waybill_number)
    WHERE return_waybill_number IS NOT NULL;
