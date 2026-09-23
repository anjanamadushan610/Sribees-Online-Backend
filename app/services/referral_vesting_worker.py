"""
SRIBEESonline — referral vesting sweep.

A referral commission is earned when the customer's twelve-hour return window
closes, not when the parcel is delivered. Something has to notice that the
window has passed, and it has to be the server: the money becoming claimable
is exactly the moment the person who earned it should be told, and a check
done when they next open the app would tell them hours or days late, if ever.

Its own loop rather than a branch inside the assistant reminder worker. The
two sweeps fail for unrelated reasons — one needs the reminder tables, this
one needs the commission tables — and sharing a loop means an exception in
either stops both. A referral sweep that silently stopped would look exactly
like "the money never vested", which is the one failure nobody would report as
a bug.
"""
import asyncio

from loguru import logger

from app.config.database import async_session_maker
from app.services.referral_service import ReferralService

# A minute. The deadline itself is twelve hours away, so this only decides how
# stale "claimable" is allowed to look -- a minute is far below what anyone
# notices, and the query is a partial-index lookup that finds nothing almost
# every time it runs.
SWEEP_INTERVAL_SECONDS = 60


async def sweep_once() -> int:
    """One pass. Returns how many commissions vested."""
    async with async_session_maker() as db:
        vested = await ReferralService.vest_due_commissions(db)

    if not vested:
        return 0

    # Tell the people who just earned something. Best-effort and deliberately
    # outside the vesting transaction: the money is theirs whether or not the
    # notification goes out, and a push failure must never roll back a vesting
    # or stop the rest of the batch being told.
    try:
        from app.services.referral_notifications import notify_vested

        async with async_session_maker() as db:
            await notify_vested(db, vested)
    except Exception:
        logger.exception(
            "Referral commissions vested but the notifications failed; "
            "the money is claimable either way"
        )

    return len(vested)


async def run_referral_vesting_worker():
    while True:
        try:
            await sweep_once()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "Referral vesting sweep failed; check migration 058 and database "
                "connectivity. Commissions stay pending until it recovers -- "
                "nothing is lost, but nobody can claim."
            )
        await asyncio.sleep(SWEEP_INTERVAL_SECONDS)
