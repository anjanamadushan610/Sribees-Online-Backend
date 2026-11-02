"""Daily chat budget: priced from usageMetadata, per customer, never named to them."""

from unittest.mock import AsyncMock

import pytest
from fakeredis.aioredis import FakeRedis

from app.config.settings import settings
from app.services import assistant_agent, chat_budget
from app.services.chat_service import ChatService
from app.services.hybrid import engine, recorder

# 1M prompt + 1M output tokens = 4.50 USD = Rs 1,350 at list price.
BIG = {"promptTokenCount": 1_000_000, "candidatesTokenCount": 600_000, "thoughtsTokenCount": 400_000}


def test_cost_counts_thinking_as_output():
    assert chat_budget.cost_lkr([BIG]) == pytest.approx(1350.0)
    assert chat_budget.cost_lkr([]) == 0


@pytest.mark.asyncio
async def test_customers_are_counted_apart(monkeypatch):
    monkeypatch.setattr(settings, "assistant_daily_budget_lkr", 100.0)
    redis = FakeRedis()
    await chat_budget.charge(redis, "user-a", [BIG])
    with pytest.raises(chat_budget.ChatBudgetExceeded):
        await chat_budget.ensure_allowed(redis, "user-a")
    await chat_budget.ensure_allowed(redis, "user-b")
    assert 0 < await redis.ttl(chat_budget._key("user-a")) <= 2 * 24 * 60 * 60


@pytest.mark.asyncio
async def test_small_turns_add_up_to_the_ceiling(monkeypatch):
    monkeypatch.setattr(settings, "assistant_daily_budget_lkr", 1.0)
    redis = FakeRedis()
    turn = {"promptTokenCount": 2_000, "candidatesTokenCount": 100}  # Rs 0.56
    await chat_budget.charge(redis, "u", [turn])
    await chat_budget.ensure_allowed(redis, "u")
    await chat_budget.charge(redis, "u", [turn])
    with pytest.raises(chat_budget.ChatBudgetExceeded):
        await chat_budget.ensure_allowed(redis, "u")


@pytest.mark.asyncio
async def test_zero_turns_the_ceiling_off(monkeypatch):
    monkeypatch.setattr(settings, "assistant_daily_budget_lkr", 0.0)
    redis = FakeRedis()
    await chat_budget.charge(redis, "u", [BIG])
    await chat_budget.ensure_allowed(redis, "u")


@pytest.mark.asyncio
async def test_agent_turn_charges_and_then_rests(monkeypatch):
    monkeypatch.setattr(settings, "assistant_daily_budget_lkr", 100.0)
    monkeypatch.setattr(engine, "mode", lambda: "off")
    monkeypatch.setattr(engine, "rescue", AsyncMock(return_value=None))
    monkeypatch.setattr(recorder, "record", lambda **_: None)
    calls = []

    async def run(**_):
        calls.append(1)
        ChatService._log_usage(BIG)
        return {"reply": "Here you go.", "language": "en", "plan_trace": []}

    monkeypatch.setattr(assistant_agent, "run", run)
    redis = FakeRedis()
    kwargs = {
        "db": AsyncMock(), "redis": redis, "session_id": "guest:d1", "user_id": None, "branch_id": None,
        "message": "sugar", "image_base64": None, "image_mime_type": None, "history": [],
        "fixed_lang": None, "fallback_lang": "en", "application_context": None,
        "briefing_prompt": "", "on_stage": None,
    }
    out = await ChatService._agent_turn(**kwargs)
    assert out["reply"] == "Here you go."
    with pytest.raises(chat_budget.ChatBudgetExceeded):
        await ChatService._agent_turn(**kwargs)
    assert len(calls) == 1
