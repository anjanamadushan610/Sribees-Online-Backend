#!/usr/bin/env python3
"""
Marx IPG payment reconciliation sweep.

Per the Marx IPG API doc's own engineering recommendation (section 7): build
a reconciliation job around GET /orders/{trId}/summary so a gateway order
left PENDING (shopper closed the app mid-3DS, the WebView confirm call never
fired, etc.) can still be resolved even if they never return to the app.

Usage:
    python scripts/reconcile_marx_payments.py

Schedule this on a cron / Windows Task Scheduler entry (every 5-10 minutes is
reasonable). Shares its logic with the admin-triggered
POST /api/v1/admin/orders/reconcile-payments endpoint via
OrderService.reconcile_pending_gateway_payments, so both drive the exact same
resolution rules.
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from loguru import logger  # noqa: E402

from app.config.database import get_db_context  # noqa: E402
from app.services.order_service import OrderService  # noqa: E402


async def main() -> None:
    async with get_db_context() as db:
        result = await OrderService.reconcile_pending_gateway_payments(db)
    logger.info(f"Marx reconciliation sweep complete: {result}")


if __name__ == "__main__":
    asyncio.run(main())
