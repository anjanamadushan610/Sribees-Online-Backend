"""
POST /banners/events — the impression/click tracking behind the branch
Marketing Dashboard's CTR column.

This endpoint shipped untested because it needs Redis branch context, and that
is exactly where its risk is: it must attribute an event to the branch the
SERVER resolved, never to anything the client sent, or one branch could inflate
(or poison) another's numbers. The dedupe rule matters just as much — without
it a customer scrolling the home carousel up and down inflates impressions and
craters every CTR a manager reads.

Everything here runs against a real Postgres and a real Redis, creates its own
rows, and removes them afterwards.
"""
import uuid
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.config.redis import RedisKeys
from app.config.settings import settings
from app.models.banner import Banner, BannerEvent
from app.models.branch import Branch
from tests.conftest import REDIS_URL, requires_db, requires_redis

pytestmark = [requires_db, requires_redis]

DEVICE = "pytest-banner-device"


async def _session_factory():
    engine = create_async_engine(settings.async_database_url, poolclass=NullPool)
    return engine, async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def _redis():
    import redis.asyncio as aioredis

    return aioredis.from_url(REDIS_URL, decode_responses=True)


async def _seed(db: AsyncSession, branch_id):
    """A live banner and one whose schedule has already ended."""
    live = Banner(
        banner_id=uuid.uuid4(), title="ZZ live", branch_id=branch_id,
        is_active=True, sort_order=900,
    )
    expired = Banner(
        banner_id=uuid.uuid4(), title="ZZ expired", branch_id=branch_id,
        is_active=True, sort_order=901,
        starts_at=datetime.now(timezone.utc) - timedelta(days=10),
        ends_at=datetime.now(timezone.utc) - timedelta(days=1),
    )
    db.add_all([live, expired])
    await db.commit()
    return live, expired


async def _cleanup(db: AsyncSession, banner_ids):
    await db.execute(delete(BannerEvent).where(BannerEvent.banner_id.in_(banner_ids)))
    await db.execute(delete(Banner).where(Banner.banner_id.in_(banner_ids)))
    await db.commit()


async def _count(db: AsyncSession, banner_id, event_type):
    return int(
        await db.scalar(
            select(func.count()).select_from(BannerEvent).where(
                BannerEvent.banner_id == banner_id,
                BannerEvent.event_type == event_type,
            )
        )
        or 0
    )


@pytest.mark.asyncio
async def test_banner_events_record_dedupe_and_scope(app_redis, app_engine_reset):
    from app.main import app

    engine, Session = await _session_factory()
    redis = await _redis()

    async with Session() as db:
        branch = (await db.execute(select(Branch).limit(1))).scalar_one_or_none()
        if branch is None:
            pytest.skip("no branches seeded in this database")
        live, expired = await _seed(db, branch.branch_id)

        # Guests resolve their branch from the X-Device-Id session key.
        await redis.setex(
            RedisKeys.branch_context(f"guest:{DEVICE}"),
            600,
            f'{{"branch_id": "{branch.branch_id}", "branch_name": "{branch.name}"}}',
        )

        headers = {"X-Device-Id": DEVICE}
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://t", timeout=30
            ) as c:
                # --- an impression is recorded ---------------------------------
                r = await c.post(
                    "/api/v1/banners/events",
                    headers=headers,
                    json={
                        "events": [{"bannerId": str(live.banner_id), "eventType": "impression"}],
                        "sessionKey": "sess-a",
                    },
                )
                assert r.status_code == 202, r.text
                assert r.json()["data"]["recorded"] == 1
                assert await _count(db, live.banner_id, "impression") == 1

                # --- the same session cannot inflate it ------------------------
                r = await c.post(
                    "/api/v1/banners/events",
                    headers=headers,
                    json={
                        "events": [{"bannerId": str(live.banner_id), "eventType": "impression"}],
                        "sessionKey": "sess-a",
                    },
                )
                assert r.status_code == 202, r.text
                assert await _count(db, live.banner_id, "impression") == 1, (
                    "a repeat impression from the same session must be collapsed"
                )

                # --- a different session is a genuinely different viewer -------
                await c.post(
                    "/api/v1/banners/events",
                    headers=headers,
                    json={
                        "events": [{"bannerId": str(live.banner_id), "eventType": "impression"}],
                        "sessionKey": "sess-b",
                    },
                )
                assert await _count(db, live.banner_id, "impression") == 2

                # --- clicks are deliberate acts and are never collapsed --------
                for _ in range(2):
                    await c.post(
                        "/api/v1/banners/events",
                        headers=headers,
                        json={
                            "events": [{"bannerId": str(live.banner_id), "eventType": "click"}],
                            "sessionKey": "sess-a",
                        },
                    )
                assert await _count(db, live.banner_id, "click") == 2

                # --- the branch comes from the SERVER, not the client ----------
                row = (
                    await db.execute(
                        select(BannerEvent).where(BannerEvent.banner_id == live.banner_id).limit(1)
                    )
                ).scalar_one()
                assert row.branch_id == branch.branch_id

                # --- an expired banner stops accruing --------------------------
                r = await c.post(
                    "/api/v1/banners/events",
                    headers=headers,
                    json={
                        "events": [{"bannerId": str(expired.banner_id), "eventType": "impression"}],
                        "sessionKey": "sess-c",
                    },
                )
                assert r.status_code == 202
                assert await _count(db, expired.banner_id, "impression") == 0, (
                    "a banner past its end date must not keep collecting impressions"
                )

                # --- an unknown id is ignored, not a 4xx -----------------------
                r = await c.post(
                    "/api/v1/banners/events",
                    headers=headers,
                    json={
                        "events": [{"bannerId": str(uuid.uuid4()), "eventType": "impression"}],
                        "sessionKey": "sess-d",
                    },
                )
                assert r.status_code == 202
                assert r.json()["data"]["recorded"] == 0

                # --- a bad event type is rejected ------------------------------
                r = await c.post(
                    "/api/v1/banners/events",
                    headers=headers,
                    json={"events": [{"bannerId": str(live.banner_id), "eventType": "nope"}]},
                )
                assert r.status_code == 422
        finally:
            await _cleanup(db, [live.banner_id, expired.banner_id])
            await redis.delete(RedisKeys.branch_context(f"guest:{DEVICE}"))
            await redis.aclose()
            await engine.dispose()


@pytest.mark.asyncio
async def test_banner_events_requires_branch_context(app_redis, app_engine_reset):
    """No delivery location chosen yet = no branch to attribute events to."""
    from app.main import app

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t", timeout=30
    ) as c:
        r = await c.post(
            "/api/v1/banners/events",
            json={"events": [{"bannerId": str(uuid.uuid4()), "eventType": "impression"}]},
        )
        # 400 either way: no device id, or a device with no resolved branch.
        assert r.status_code == 400, r.text
