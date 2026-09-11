"""
GET /delivery/slots — the dispatch rounds a customer can order into.

The rule worth protecting: a run whose ordering cut-off has already passed must
be **absent**, not returned flagged unavailable. The van is loaded, nothing more
can go on it, and offering a slot checkout would refuse is worse than offering
none. That is also why the same branch's list is shorter later in the day.

The second rule: `configured: false` with an empty list means "this branch has
not set its rounds up", which is a completely different message to the customer
than "everything for today is closed". The app must not show one screen for both.
"""
import json
import uuid

import httpx
import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.config.redis import RedisKeys
from app.config.settings import settings
from app.models.branch import Branch
from tests.conftest import REDIS_URL, requires_db, requires_redis

pytestmark = [requires_db, requires_redis]

DEVICE = "pytest-slots-device"

# Two runs far enough apart that "before both" and "after the first" are
# unambiguous whatever time of day CI happens to run.
RUNS = [
    {"label": "Morning", "dispatch_time": "10:00", "cutoff_minutes": 30},
    {"label": "Afternoon", "dispatch_time": "14:00", "cutoff_minutes": 30},
    {"label": "Evening", "dispatch_time": "18:00", "cutoff_minutes": 30},
]


async def _redis():
    import redis.asyncio as aioredis

    return aioredis.from_url(REDIS_URL, decode_responses=True)


@pytest.mark.asyncio
async def test_delivery_slots_reflect_cutoffs_and_configuration(app_redis, app_engine_reset, test_branch):
    from app.main import app

    engine = create_async_engine(settings.async_database_url, poolclass=NullPool)
    Session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    redis = await _redis()

    async with Session() as db:
        branch = test_branch
        original = list(branch.delivery_runs or [])

        await redis.setex(
            RedisKeys.branch_context(f"guest:{DEVICE}"),
            600,
            json.dumps({"branch_id": str(branch.branch_id), "branch_name": branch.name}),
        )
        headers = {"X-Device-Id": DEVICE}

        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://t", timeout=30
            ) as c:
                # ---- configured branch -------------------------------------
                await db.execute(
                    update(Branch)
                    .where(Branch.branch_id == branch.branch_id)
                    .values(delivery_runs=RUNS)
                )
                await db.commit()

                r = await c.get("/api/v1/delivery/slots?days=2", headers=headers)
                assert r.status_code == 200, r.text
                data = r.json()["data"]
                assert data["configured"] is True
                assert data["branch"]["branchId"] == str(branch.branch_id)

                slots = data["slots"]
                assert slots, "a configured branch over 2 days must offer something"

                # Every slot offered is still orderable...
                assert all(s["minutesToCutoff"] > 0 for s in slots), (
                    "a slot past its cut-off must be omitted, never offered"
                )
                # ...and today's list can only ever be a suffix of the timetable,
                # never more runs than the branch actually has per day.
                today = [s for s in slots if s["isToday"]]
                assert len(today) <= len(RUNS)

                # Ordered by when they close, so the app can render a queue.
                assert [s["orderBy"] for s in slots] == sorted(s["orderBy"] for s in slots)

                for s in slots:
                    assert s["orderBy"] < s["dispatchAt"], (
                        "ordering must close before the van leaves"
                    )
                    assert s["label"] in {r["label"] for r in RUNS}

                # ---- a branch that has not set its rounds up ----------------
                await db.execute(
                    update(Branch)
                    .where(Branch.branch_id == branch.branch_id)
                    .values(delivery_runs=[])
                )
                await db.commit()

                r = await c.get("/api/v1/delivery/slots", headers=headers)
                assert r.status_code == 200, r.text
                data = r.json()["data"]
                assert data["slots"] == []
                assert data["configured"] is False, (
                    "'not set up' must be distinguishable from 'closed for today'"
                )

                # ---- a malformed row costs one slot, not the whole request --
                await db.execute(
                    update(Branch)
                    .where(Branch.branch_id == branch.branch_id)
                    .values(
                        delivery_runs=[
                            {"label": "Broken", "dispatch_time": "not-a-time"},
                            {"label": "Good", "dispatch_time": "23:30", "cutoff_minutes": 5},
                        ]
                    )
                )
                await db.commit()

                r = await c.get("/api/v1/delivery/slots?days=2", headers=headers)
                assert r.status_code == 200, "one bad row must not 500 the checkout"
                assert all(s["label"] == "Good" for s in r.json()["data"]["slots"])
        finally:
            await db.execute(
                update(Branch)
                .where(Branch.branch_id == branch.branch_id)
                .values(delivery_runs=original)
            )
            await db.commit()
            await redis.delete(RedisKeys.branch_context(f"guest:{DEVICE}"))
            await redis.aclose()
            await engine.dispose()


@pytest.mark.asyncio
async def test_delivery_slots_requires_branch_context(app_redis, app_engine_reset, test_branch):
    """A shopper with no delivery location chosen has no timetable to show."""
    from app.main import app

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t", timeout=30
    ) as c:
        r = await c.get("/api/v1/delivery/slots")
        assert r.status_code == 400, r.text

        # A device id that has never resolved a branch is equally unusable.
        r = await c.get(
            "/api/v1/delivery/slots", headers={"X-Device-Id": f"unknown-{uuid.uuid4()}"}
        )
        assert r.status_code == 400, r.text
