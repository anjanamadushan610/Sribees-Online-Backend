-- =============================================================================
-- Migration 059: give already-vested commissions a deadline in the past.
--
-- 058 added `claimable_at` and backfilled it for PENDING rows. The claim path
-- now also requires `claimable_at <= now()`, as a second lock on top of the
-- status so a row vested early cannot also be paid early.
--
-- Those two together broke the rows that were ALREADY `claimable`. They vested
-- under the old rule -- the moment their order was delivered -- so they never
-- got a deadline, and the new guard reads a NULL deadline as "not due yet".
-- Every commission anyone had already earned quietly became unclaimable, with
-- the app showing the balance and the claim finding nothing to pay.
--
-- The fix is not to relax the guard. These commissions were genuinely earned
-- and promised under the rule in force at the time, and the honest record of
-- that is a deadline that has already passed -- not a NULL the claim path has
-- to make an exception for. An exception would also be a hole: a row with no
-- deadline is exactly what a bug in the vesting path would produce.
--
-- Dated from the delivery where we have one, so the row reads truthfully about
-- when the money was earned, and clamped to now() so nothing is pushed into
-- the future and un-claimed by this repair.
--
-- Touches only rows already CLAIMABLE with no deadline: pending rows keep
-- waiting, paid and reversed rows are finished.
--
-- SQLAlchemy create_all() needs nothing here. Idempotent -- re-running finds
-- no rows left to fix.
-- =============================================================================

UPDATE referral_commissions rc
SET    claimable_at = LEAST(NOW(), COALESCE(o.delivered_at + INTERVAL '12 hours', NOW()))
FROM   orders o
WHERE  rc.order_id = o.order_id
  AND  rc.status = 'claimable'
  AND  rc.claimable_at IS NULL;

-- Any claimable row whose order has since been deleted would be stranded by
-- the join above. Rare, but the cost of missing one is somebody's money.
UPDATE referral_commissions
SET    claimable_at = NOW()
WHERE  status = 'claimable'
  AND  claimable_at IS NULL;
