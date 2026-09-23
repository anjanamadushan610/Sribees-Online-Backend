-- =============================================================================
-- Migration 058: a referral commission waits for the return window to close.
--
-- Commissions vested the moment an order was marked delivered. The customer
-- then had twelve hours to send the goods back, and by the time they did the
-- referrer could already have moved the money into their wallet and spent it.
-- The clawback exists, but it pushes a wallet negative to collect a debt from
-- someone who did nothing wrong -- their friend returned a kettle.
--
-- `claimable_at` is when the money genuinely becomes theirs: delivery plus the
-- same twelve-hour window the customer gets, plus a small buffer so the two
-- deadlines cannot race. Until then the row stays `pending` and nothing can
-- move it.
--
-- Stored rather than computed from the order, because the window is a property
-- of the commission at the time it was earned. Changing the platform's return
-- window later must not retroactively move money that was already promised --
-- the same reason `rate_percentage` is snapshotted onto the row.
--
-- `returned_subtotal` supports a PARTIAL reversal. A customer returning one
-- item out of five should cost the referrer the commission on that one item,
-- not all of it: the other four were delivered and kept.
--
-- Both NULL/0 on every existing row, so nothing already earned changes.
--
-- SQLAlchemy create_all() adds these on a fresh DB. Idempotent -- the deploy
-- applies this file again on each release.
-- =============================================================================

ALTER TABLE referral_commissions
    ADD COLUMN IF NOT EXISTS claimable_at TIMESTAMPTZ;

ALTER TABLE referral_commissions
    ADD COLUMN IF NOT EXISTS returned_subtotal NUMERIC(10, 2) NOT NULL DEFAULT 0;

-- The sweeper's only query: "which pending rows are due now". Partial, so it
-- stays small however many commissions accumulate -- a row leaves the index
-- for good once it vests.
CREATE INDEX IF NOT EXISTS ix_referral_commissions_due
    ON referral_commissions (claimable_at)
    WHERE status = 'pending' AND claimable_at IS NOT NULL;

-- Existing pending rows for orders already delivered would otherwise sit
-- forever with a NULL claimable_at and never vest. Give them the window they
-- would have had, measured from the delivery that already happened.
UPDATE referral_commissions rc
SET    claimable_at = o.delivered_at + INTERVAL '12 hours'
FROM   orders o
WHERE  rc.order_id = o.order_id
  AND  rc.claimable_at IS NULL
  AND  rc.status = 'pending'
  AND  o.delivered_at IS NOT NULL;
