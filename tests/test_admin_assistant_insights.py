"""Admin view of the hybrid assistant (P3)."""

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fakeredis.aioredis import FakeRedis
from fastapi import HTTPException

from app.api.v1 import admin_assistant_insights as api
from app.services.hybrid import engine
from app.services.hybrid.plan import Plan


@pytest.mark.asyncio
async def test_insights_carry_the_live_settings(monkeypatch):
    monkeypatch.setattr(api.insights, "summary", AsyncMock(return_value={"turns": 3, "rules": {}}))
    monkeypatch.setattr(api.settings, "hybrid_mode", "shadow")
    redis = FakeRedis()
    await redis.sadd(engine.DISABLED_RULES_KEY, "cart")
    out = await api.get_insights(days=7, db=AsyncMock(), redis=redis, _admin=None)
    cfg = out["data"]["config"]
    assert out["data"]["turns"] == 3 and cfg["hybrid_mode"] == "shadow" and cfg["disabled_rules"] == ["cart"]


@pytest.mark.asyncio
async def test_switching_a_rule_off_and_on_takes_effect_without_a_restart():
    redis = FakeRedis()
    plan = Plan([], "want", "L2", "products.want", "want|<P>")
    await api.set_rule("products", api.RuleUpdate(enabled=False), redis=redis, _admin=None)
    assert not engine.answer_allowed(plan, await engine.disabled_rules(redis))
    await api.set_rule("products", api.RuleUpdate(enabled=True), redis=redis, _admin=None)
    assert engine.answer_allowed(plan, await engine.disabled_rules(redis))


@pytest.mark.asyncio
async def test_unknown_rules_and_patterns_are_refused():
    with pytest.raises(HTTPException) as exc:
        await api.set_rule("drop.tables", api.RuleUpdate(enabled=False), redis=FakeRedis(), _admin=None)
    assert exc.value.status_code == 404
    db = AsyncMock()
    db.get.return_value = None
    with pytest.raises(HTTPException):
        await api.update_pattern(uuid.uuid4(), api.PatternUpdate(status="active"), db=db, _admin=None)


@pytest.mark.asyncio
async def test_an_admin_can_disable_a_learned_pattern(monkeypatch):
    invalidated = []
    monkeypatch.setattr(api.memory, "invalidate", lambda: invalidated.append(True))
    pattern = SimpleNamespace(
        pattern_id=uuid.uuid4(), text_key="sini one", plan={"steps": []}, status="active", source="model", examples=4,
        shadow_agree=20, shadow_disagree=0, canary_agree=1, canary_disagree=0, created_at=None, promoted_at=None,
    )
    db = AsyncMock()
    db.get.return_value = pattern
    out = await api.update_pattern(pattern.pattern_id, api.PatternUpdate(status="disabled"), db=db, _admin=None)
    assert out["data"]["status"] == "disabled" and invalidated
    assert out["data"]["shadow"]["rate"] == 1.0


def test_the_endpoints_require_an_admin():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.config.database import get_db
    from app.config.redis import get_redis

    app = FastAPI()
    app.include_router(api.router, prefix="/api/v1/admin/assistant-insights")
    app.dependency_overrides[get_redis] = lambda: FakeRedis()
    app.dependency_overrides[get_db] = lambda: AsyncMock()  # auth stays real
    client = TestClient(app)
    responses = [
        client.get("/api/v1/admin/assistant-insights"),
        client.get("/api/v1/admin/assistant-insights/patterns"),
        client.put("/api/v1/admin/assistant-insights/rules/cart", json={"enabled": False}),
    ]
    assert all(r.status_code in (401, 403) for r in responses), [r.status_code for r in responses]
