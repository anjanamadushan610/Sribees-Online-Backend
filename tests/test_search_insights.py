"""
Missed-search recording and the AI keyword budget.

Both sit beside the search path rather than on it, and both exist to hold a
line — the inbox against keystroke noise, the budget against runaway Gemini
cost — so the tests are about exactly those lines.
"""
import asyncio

import pytest

from app.services import keyword_suggestion_service as kw
from app.services import search_insights_service as insights


class FakeRedis:
    """The handful of redis.asyncio calls these services make, in memory."""

    def __init__(self):
        self.data: dict[str, str] = {}

    async def get(self, key):
        return self.data.get(key)

    async def set(self, key, value, ex=None, nx=False):
        if nx and key in self.data:
            return None
        self.data[key] = str(value)
        return True

    async def incr(self, key):
        self.data[key] = str(int(self.data.get(key, 0)) + 1)
        return int(self.data[key])

    async def decr(self, key):
        self.data[key] = str(int(self.data.get(key, 0)) - 1)
        return int(self.data[key])

    async def expire(self, key, seconds):
        return True


class FakeSession:
    def __init__(self, log):
        self.log = log

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, statement):
        self.log.append(statement.compile().params["query_key"])

    async def commit(self):
        pass


@pytest.fixture
def recorded(monkeypatch):
    monkeypatch.setattr(insights, "SETTLE_SECONDS", 0.05)
    log: list[str] = []
    return log, (lambda: FakeSession(log))


# ---------------------------------------------------------------------------
# What counts as a miss
# ---------------------------------------------------------------------------

def test_scripts_and_spellings_share_one_inbox_row():
    assert insights.miss_key("Seeni") == insights.miss_key("සීනි") == insights.miss_key("sini")
    assert insights.miss_key("rasa kavili") == insights.miss_key("Rasakavili")


@pytest.mark.parametrize("query", ["a", "ab", "500", "x" * 100, "a b c d e f g h"])
def test_noise_is_not_recorded(query):
    assert insights.miss_key(query) is None


async def test_search_that_found_something_is_not_a_miss(recorded):
    log, maker = recorded
    await insights.note_search(maker, FakeRedis(), "guest:1", "kiri", 3, None)
    assert log == []


async def test_miss_is_recorded_once_typing_stops(recorded):
    log, maker = recorded
    await insights.note_search(maker, FakeRedis(), "guest:1", "kottu mee", 0, None)
    assert log == ["kotumi"]


async def test_word_on_its_way_somewhere_is_not_recorded(recorded):
    log, maker = recorded
    redis = FakeRedis()
    first = asyncio.create_task(insights.note_search(maker, redis, "guest:1", "kot", 0, None))
    await asyncio.sleep(0.01)
    await insights.note_search(maker, redis, "guest:1", "kottu", 0, None)
    await first
    assert log == ["kotu"]


async def test_one_shopper_counts_once_a_day(recorded):
    log, maker = recorded
    redis = FakeRedis()
    for _ in range(3):
        await insights.note_search(maker, redis, "guest:1", "kottu", 0, None)
    await insights.note_search(maker, redis, "guest:2", "kottu", 0, None)
    assert log == ["kotu", "kotu"]


async def test_recording_failure_never_raises(recorded):
    class Broken(FakeSession):
        async def execute(self, statement):
            raise RuntimeError("db down")

    await insights.note_search(lambda: Broken([]), FakeRedis(), "guest:1", "kottu", 0, None)


# ---------------------------------------------------------------------------
# The Gemini budget
# ---------------------------------------------------------------------------

@pytest.fixture
def gemini(monkeypatch):
    calls: list[str] = []

    async def fake_suggest(name, description=None, category=None):
        calls.append(name)
        if name == "boom":
            raise kw.GeminiAPIError("upstream 500")
        return "kesel, kehel"

    monkeypatch.setattr(kw, "suggest_search_keywords", fake_suggest)
    monkeypatch.setattr(kw.settings, "gemini_keyword_daily_limit", 2)
    return calls


async def test_repeat_request_is_served_from_cache(gemini):
    redis = FakeRedis()
    first = await kw.suggest_within_budget(redis, "Ambul Banana")
    again = await kw.suggest_within_budget(redis, "  ambul banana ")
    assert gemini == ["Ambul Banana"]
    assert (first["cached"], again["cached"]) == (False, True)
    assert again["keywords"] == "kesel, kehel"
    assert again["remaining_today"] == 1


async def test_daily_limit_is_a_hard_ceiling(gemini):
    redis = FakeRedis()
    await kw.suggest_within_budget(redis, "one")
    await kw.suggest_within_budget(redis, "two")
    with pytest.raises(kw.SuggestionBudgetExceeded):
        await kw.suggest_within_budget(redis, "three")
    assert gemini == ["one", "two"]


async def test_failed_call_gives_its_slot_back(gemini):
    redis = FakeRedis()
    with pytest.raises(kw.GeminiAPIError):
        await kw.suggest_within_budget(redis, "boom")
    await kw.suggest_within_budget(redis, "one")
    await kw.suggest_within_budget(redis, "two")
    assert gemini == ["boom", "one", "two"]


async def test_zero_limit_turns_the_feature_off(gemini, monkeypatch):
    monkeypatch.setattr(kw.settings, "gemini_keyword_daily_limit", 0)
    with pytest.raises(kw.SuggestionBudgetExceeded):
        await kw.suggest_within_budget(FakeRedis(), "one")
    assert gemini == []
