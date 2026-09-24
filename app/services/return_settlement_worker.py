"""
SRIBEESonline — settle reverse pickups whose goods are back.

A reverse-pickup refund waits for the return leg to reach `delivered`, which
happens when our counter gives the rider the handover code. The webhook that
says so is not retried by SribeesExpress, so this loop asks for every open
return every few minutes and settles what has landed.

Its own loop for the same reason as the others: a refund loop that silently
stopped would look exactly like "the refund never came".
"""
import asyncio

from loguru import logger

from app.config.database import async_session_maker
from app.services import courier_sync_service

SWEEP_INTERVAL_SECONDS = 300


async def sweep_once() -> int:
    """One pass. Returns how many returns were refunded."""
    async with async_session_maker() as db:
        return await courier_sync_service.sweep_open_returns(db)


async def run_return_settlement_worker():
    while True:
        try:
            refunded = await sweep_once()
            if refunded:
                logger.info(f"Return sweep refunded {refunded} return(s)")
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "Return settlement sweep failed; approved reverse pickups stay "
                "in QC Pending until it recovers -- nothing is lost."
            )
        await asyncio.sleep(SWEEP_INTERVAL_SECONDS)
