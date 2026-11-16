"""Verify production wallet-change configuration without creating a credit.

Run inside the deployed Online app container. Only configuration readiness,
public courier origins and aggregate queue counts are printed.
"""
import asyncio
import json
import sys

from sqlalchemy import select

from app.config.database import async_session_maker
from app.config.settings import settings
from app.models.wallet import CourierWalletCredit
from app.services import courier_credentials
from app.services.courier_service import CourierService


async def verify():
    if settings.app_env.lower() != "production":
        raise RuntimeError("Expected production container")
    if not settings.rider_wallet_change_enabled or not settings.finance_outbox_enabled:
        raise RuntimeError("Required wallet-change configuration is disabled")
    async with async_session_maker() as db:
        await db.execute(select(CourierWalletCredit.source).limit(0))
        targets = await courier_credentials.all_targets(db)
    if not targets:
        raise RuntimeError("No courier account is configured")
    for target in targets:
        async with courier_credentials.using(target):
            pending = await CourierService._request("GET", "/wallet-change/pending")
        if not isinstance(pending, list):
            raise RuntimeError("Unexpected courier response")
        print(json.dumps({"courier_origin": target.base_url, "environment": target.environment,
                          "pending_requests": len(pending), "wallet_api": "ready"}))
    print("production_wallet_change_ready")


if __name__ == "__main__":
    try:
        asyncio.run(verify())
    except Exception as exc:
        print(f"production_wallet_change_verification_failed: {type(exc).__name__}")
        sys.exit(1)
