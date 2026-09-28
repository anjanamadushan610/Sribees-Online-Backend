"""
Runs the payment sweep every minute (app/payments/service.py::sweep).

Before this, settling a card payment whose customer never came back needed a
cron job nobody had scheduled, or an admin pressing a button. A payment that
silently never settles looks exactly like "the customer paid and got nothing".
"""
import asyncio

from loguru import logger

from app.config.database import async_session_maker
from app.payments import service

SWEEP_INTERVAL_SECONDS = 60


async def run_payment_worker():
    while True:
        try:
            async with async_session_maker() as db:
                await service.sweep(db)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "Payment sweep failed; check migration 076 and the gateway. Unpaid "
                "orders stay held and paid ones unconfirmed until it recovers."
            )
        await asyncio.sleep(SWEEP_INTERVAL_SECONDS)
