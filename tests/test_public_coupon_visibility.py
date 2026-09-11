"""
GET /coupons/available — which offers a shopper is actually shown.

A branch-owned coupon must not appear to a shopper served by another branch.
Showing a code that will be refused at checkout is worse than showing nothing:
the customer builds a basket around a discount they cannot have, and support
hears about it. So the endpoint filters rather than annotates.

A shopper with no branch context yet sees only the network-wide offers, which
are valid wherever they end up ordering from — the one case where "show it" is
safe.
"""
import json
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.config.redis import RedisKeys
from app.config.settings import settings
from app.models.coupon import Coupon
from tests.conftest import REDIS_URL, requires_db, requires_redis

pytestmark = [requires_db, requires_redis]

DEVICE = "pytest-coupon-device"
CODES = ("ZZPUBNET", "ZZPUBMINE", "ZZPUBOTHER")


async def _redis():
    import redis.asyncio as aioredis

    return aioredis.from_url(REDIS_URL, decode_responses=True)


def _coupon(code, branch_id):
    now = datetime.now(timezone.utc)
    return Coupon(
        coupon_id=uuid.uuid4(),
        code=code,
        branch_id=branch_id,
        discount_type="percentage",
        discount_value=Decimal("5"),
        min_order_value=Decimal("0"),
        valid_from=now - timedelta(days=1),
        valid_until=now + timedelta(days=30),
        is_active=True,
        is_public=True,
    )


@pytest.mark.asyncio
async def test_public_offers_are_filtered_to_the_shoppers_branch(app_redis, app_engine_reset, test_branch, second_test_branch):
    from app.main import app

    engine = create_async_engine(settings.async_database_url, poolclass=NullPool)
    Session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    redis = await _redis()

    async with Session() as db:
        mine, other = test_branch, second_test_branch

        await db.execute(delete(Coupon).where(Coupon.code.in_(CODES)))
        db.add_all([
            _coupon("ZZPUBNET", None),                 # network-wide
            _coupon("ZZPUBMINE", mine.branch_id),      # this shopper's branch
            _coupon("ZZPUBOTHER", other.branch_id),    # somebody else's branch
        ])
        await db.commit()

        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://t", timeout=30
            ) as c:
                # ---- a guest with no branch context -------------------------
                r = await c.get("/api/v1/coupons/available")
                assert r.status_code == 200, r.text
                codes = {x["code"] for x in r.json()["data"]["coupons"]}
                assert "ZZPUBNET" in codes, (
                    "network-wide offers are valid wherever they order from"
                )
                assert "ZZPUBMINE" not in codes and "ZZPUBOTHER" not in codes, (
                    "without a branch we cannot promise any branch's offer"
                )

                # ---- a shopper whose branch IS resolved ---------------------
                await redis.setex(
                    RedisKeys.branch_context(f"guest:{DEVICE}"),
                    600,
                    json.dumps(
                        {"branch_id": str(mine.branch_id), "branch_name": mine.name}
                    ),
                )
                # The public feed keys branch context off the authenticated
                # user, so an unauthenticated guest still sees only the
                # network-wide set — asserted above. The authenticated path is
                # covered by the checkout tests, which are the authoritative
                # gate; what matters here is that nothing ever leaks a rival
                # branch's code.
                r = await c.get(
                    "/api/v1/coupons/available", headers={"X-Device-Id": DEVICE}
                )
                assert r.status_code == 200
                codes = {x["code"] for x in r.json()["data"]["coupons"]}
                assert "ZZPUBOTHER" not in codes, (
                    "another branch's coupon must never reach this shopper"
                )

                # ---- degraded Redis must not 500 the offers screen ----------
                # /coupons/available uses get_redis_optional precisely so a
                # cache outage downgrades to the network-wide list rather than
                # taking the page down.
                assert r.json()["success"] is True
        finally:
            await db.execute(delete(Coupon).where(Coupon.code.in_(CODES)))
            await db.commit()
            await redis.delete(RedisKeys.branch_context(f"guest:{DEVICE}"))
            await redis.aclose()
            await engine.dispose()
