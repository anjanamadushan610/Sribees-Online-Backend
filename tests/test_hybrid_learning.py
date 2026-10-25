"""The learner (P2): exact memory, what may be learned, shadow counting, canaries."""

import time
import uuid
from unittest.mock import AsyncMock

import pytest

from app.services import assistant_agent
from app.services.chat_service import ChatService
from app.services.hybrid import canary, engine, memory, miner, recorder
from app.services.hybrid.plan import Plan, ToolCall

SEARCH = {"steps": [{"name": "search_products", "args": {"query": "sugar"}}], "reply_kind": "list"}


@pytest.fixture
def remembered(monkeypatch):
    def put(status):
        key = recorder.scrub("sini godak one")
        monkeypatch.setattr(memory, "_cache", {key: ("p-1", status, SEARCH)})
        monkeypatch.setattr(memory, "_loaded_at", time.monotonic())
        return key

    return put


@pytest.mark.asyncio
async def test_memory_finds_a_message_by_its_folded_key(remembered):
    remembered("active")
    plan = await memory.lookup(None, "Sini  GODAK one!", "si")
    assert plan.layer == "L1" and plan.rule == "pattern:p-1" and plan.steps[0].args == {"query": "sugar"}
    assert await memory.lookup(None, "something else", "si") is None


@pytest.mark.asyncio
async def test_only_active_patterns_may_answer(remembered):
    remembered("shadow")
    assert not engine.answer_allowed(await memory.lookup(None, "sini godak one", "si"))
    remembered("active")
    assert engine.answer_allowed(await memory.lookup(None, "sini godak one", "si"))


def test_only_reads_without_personal_arguments_are_learned():
    assert memory.replayable([{"name": "search_products", "args": {"query": "x"}}])
    assert memory.replayable([{"name": "my_orders", "args": {"recent": True}}])
    assert not memory.replayable([{"name": "my_orders", "args": {"order_reference": "SB-1"}}])
    assert not memory.replayable([{"name": "stage_basket_change", "args": {"operations": []}}])
    assert not memory.replayable([{"name": "stage_wishlist_add", "args": {"product_id": "x"}}])
    assert not memory.replayable([])


def test_plan_shape_ignores_order_repeats_and_spelling():
    a = [{"name": "search_products", "args": {"query": "Kiri Piti"}}, {"name": "my_orders", "args": {}}]
    b = [{"name": "my_orders", "args": {}}, {"name": "search_products", "args": {"query": "kiri  piti"}}]
    assert miner.shape(a) == miner.shape(b)
    assert miner.shape(a) != miner.shape([{"name": "search_products", "args": {"query": "seeni"}}])


async def _chat(monkeypatch, status, agent_trace):
    from app.services import assistant_briefings

    monkeypatch.setattr(assistant_agent.settings, "assistant_agent_v2", True)
    monkeypatch.setattr(assistant_agent.settings, "hybrid_mode", "active")
    monkeypatch.setattr(assistant_briefings, "live_for", AsyncMock(return_value=[]))
    plan = Plan([ToolCall("search_products", {"query": "sugar"})], "list", "L1", "pattern:p-1", "k", language="si", pattern_status=status)
    monkeypatch.setattr(engine, "plan_for", AsyncMock(return_value=plan))
    agent = AsyncMock(return_value={
        "reply": "ok", "products": [], "orders": [], "actions": [], "guide": None, "panels": [],
        "language": "si", "search_context": None, "briefing_note": None, "briefing_ids": [], "plan_trace": agent_trace,
    })
    monkeypatch.setattr(assistant_agent, "run", agent)
    monkeypatch.setattr(recorder, "record", lambda **row: None)
    counted = []

    async def count(rule, field):
        counted.append((rule, field))

    monkeypatch.setattr(memory, "count", count)
    scheduled = []

    def background(coro):
        scheduled.append(coro)

    monkeypatch.setattr(recorder, "background", background)
    redis = AsyncMock()
    redis.get.return_value = None
    await ChatService.chat(AsyncMock(), redis, "guest:x", uuid.uuid4(), "sini godak one", lang="si")
    for coro in scheduled:
        await coro
    return agent, counted


@pytest.mark.asyncio
async def test_a_shadow_pattern_is_compared_with_the_model_and_counted(monkeypatch):
    agent, counted = await _chat(monkeypatch, "shadow", [{"name": "search_products", "args": {"query": "sugar 1kg"}}])
    assert agent.called
    assert counted == [("pattern:p-1", "shadow_agree")]
    agent, counted = await _chat(monkeypatch, "shadow", [{"name": "my_orders", "args": {}}])
    assert counted == [("pattern:p-1", "shadow_disagree")]


def test_canary_rate_decides_whether_a_fast_answer_is_rechecked(monkeypatch):
    calls = []

    async def fake_check(plan, kwargs, record):
        calls.append(plan)

    monkeypatch.setattr(canary, "_check", fake_check)
    plan = Plan([], "greeting", "L2", "social.greeting", "greeting")
    monkeypatch.setattr(canary.settings, "hybrid_canary_rate", 0.0)
    assert canary.maybe_check(plan, {}, {}) is False
    monkeypatch.setattr(canary.settings, "hybrid_canary_rate", 1.0)
    assert canary.maybe_check(plan, {}, {}) is False  # no running loop: skipped safely


@pytest.mark.asyncio
async def test_canary_schedules_inside_a_running_loop(monkeypatch):
    calls = []

    async def fake_check(plan, kwargs, record):
        calls.append(plan.rule)

    monkeypatch.setattr(canary, "_check", fake_check)
    monkeypatch.setattr(canary.settings, "hybrid_canary_rate", 1.0)
    assert canary.maybe_check(Plan([], "greeting", "L2", "social.greeting", "greeting"), {}, {}) is True
    for task in list(canary._pending):
        await task
    assert calls == ["social.greeting"]
