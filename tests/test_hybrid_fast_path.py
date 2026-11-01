"""The free fast path: rules, execution, privacy of the log, and how chat uses it."""

import json
import uuid
from collections import Counter
from unittest.mock import AsyncMock

import pytest

from app.services import assistant_agent
from app.services.catalog_search_service import Vocabulary
from app.services.chat_service import ChatService
from app.services.hybrid import engine, recorder
from app.services.hybrid.normalize import parse
from app.services.hybrid.plan import Plan, ToolCall
from app.services.hybrid.rules import route
from app.services.search_text import index_tokens

CATALOG = {
    "p-sugar": ("White Sugar 1kg", "සුදු සීනි 1kg", "seeni, sugar"),
    "p-dhal": ("Red Dhal 1kg", "පරිප්පු 1kg", "parippu, dhal"),
    "p-anchor": ("Anchor Full Cream Milk Powder 400g", "Anchor ෆුල් ක්‍රීම් කිරිපිටි 400g", "kiripiti"),
    "p-coil": ("Ninja Mosquito Coils 10 Hours", "Ninja මදුරු දඟර පැය 10", "maduru dagara"),
}


@pytest.fixture(autouse=True)
def _every_word_found(monkeypatch):
    """No database here; the coverage check has its own tests below."""
    from app.services import catalog_search_service

    monkeypatch.setattr(catalog_search_service, "any_covers_all", AsyncMock(return_value=True))


@pytest.fixture(scope="module")
def vocab():
    counts = Counter()
    for name, si, kw in CATALOG.values():
        counts.update(set(index_tokens(name, si, None, kw)[0]))
    return Vocabulary(counts)


def plan_of(text, vocab, fallback="si"):
    return route(parse(text), vocab, fallback)


@pytest.mark.parametrize(
    "text,rule,first_tool",
    [
        ("seeni", "products.list", "search_products"),
        ("mata seeni kilo ekak one", "products.want", "search_products"),
        ("Anchor 400g kiripiti kiyada?", "products.price", "search_products"),
        ("maduru dagara thiyenawada", "products.want", "search_products"),
        ("mage order eka koheda", "orders.track", "my_orders"),
        ("where is my order SB-10423", "orders.track", "my_orders"),
        ("kalin gaththa badu pennanna", "orders.history", "my_orders"),
        ("cart eka open karanna", "help.open", "app_help"),
        ("address eka wenas karanne kohomada", "help.how", "app_help"),
        ("order ekak danne kohomada", "help.order", "app_help"),
        ("seeni 2k cart ekata danna", "cart.add", "stage_basket_change"),
    ],
)
def test_rules_take_the_frequent_safe_shapes(vocab, text, rule, first_tool):
    plan = plan_of(text, vocab)
    assert plan is not None and plan.rule == rule
    assert plan.steps[0].name == first_tool


@pytest.mark.parametrize(
    "text",
    [
        "seeni epa, parippu one",          # negation
        "Anchor da Lakspray da hoda",     # comparison / judgement
        "mata cake ekak hadanna one",     # recipe
        "seeni one, mage order eka koheda",  # two kinds of request
        "kiri gana 3 karanna",            # a count without "add" changes the basket
        "mage order eka cancel karanna puluwanda",
        "hisaradayata monawada hoda",     # need-based, judgement
        "mage order eka kohomada",        # status or how-to? not help
    ],
)
def test_anything_needing_judgement_goes_to_the_model(vocab, text):
    plan = plan_of(text, vocab)
    assert plan is None or plan.rule == "orders.track"


def test_sizes_become_a_filter_and_lists_split(vocab):
    plan = plan_of("Anchor 400g kiripiti kiyada", vocab)
    assert plan.steps[0].args == {"query": "Anchor kiripiti", "size": "400g"}
    plan = plan_of("seeni, parippu", vocab)
    assert [s.args["query"] for s in plan.steps] == ["seeni", "parippu"]


def test_language_follows_the_words(vocab):
    assert plan_of("do you have sugar", vocab).language == "en"
    assert plan_of("seeni one", vocab, fallback="en").language == "si"
    assert plan_of("hi", vocab, fallback="ta").language == "ta"  # neutral keeps the conversation's


def _turn_tool(results):
    async def run_tool(name, args, ctx, turn):
        found = results.get(args.get("query"), [])
        turn.register(found, from_search=True)
        return {"query": args.get("query"), "products": [assistant_agent._lean_product(p) for p in found]}

    return run_tool


SUGAR = {"product_id": "p-sugar", "name": "සුදු සීනි 1kg", "price": 260, "discount_price": None, "is_on_sale": False, "variants": []}


@pytest.mark.asyncio
async def test_run_plan_answers_from_real_results(monkeypatch):
    monkeypatch.setattr(assistant_agent, "_run_tool", _turn_tool({"seeni": [SUGAR]}))
    plan = Plan([ToolCall("search_products", {"query": "seeni"})], "price", "L2", "products.price", "price|<P>", language="si")
    out = await engine.run_plan(plan, db=None, redis=None, user_id=None, branch_id=uuid.uuid4(), history=[], emit=AsyncMock())
    assert "සුදු සීනි 1kg" in out["reply"] and "260" in out["reply"]
    assert [p["product_id"] for p in out["products"]] == ["p-sugar"]


@pytest.mark.asyncio
async def test_run_plan_hands_over_when_nothing_was_found(monkeypatch):
    monkeypatch.setattr(assistant_agent, "_run_tool", _turn_tool({}))
    plan = Plan([ToolCall("search_products", {"query": "zzz"})], "list", "L2", "products.list", "list|<P>", language="si")
    assert await engine.run_plan(plan, db=None, redis=None, user_id=None, branch_id=uuid.uuid4(), history=[], emit=AsyncMock()) is None


@pytest.mark.asyncio
async def test_run_plan_asks_for_a_location_first(monkeypatch):
    async def run_tool(name, args, ctx, turn):
        turn.actions.append({"target": "addresses"})
        return {"error": "No delivery location is selected"}

    monkeypatch.setattr(assistant_agent, "_run_tool", run_tool)
    plan = Plan([ToolCall("search_products", {"query": "seeni"})], "list", "L2", "products.list", "list|<P>", language="en")
    out = await engine.run_plan(plan, db=None, redis=None, user_id=None, branch_id=None, history=[], emit=AsyncMock())
    assert "delivery location" in out["reply"] and out["actions"][0]["target"] == "addresses"


def test_log_text_is_scrubbed():
    key = recorder.scrub("mage order SB-10423 koheda? call 0771234567 or me@example.com id 3f2b8c1e-1111-2222-3333-444455556666")
    for leak in ("10423", "0771234567", "example", "3f2b8c1e"):
        assert leak not in key
    assert "<order>" in key and "<phone>" in key and "<email>" in key and "<id>" in key
    assert recorder.scrub("සීනි ඕන") == "sini ona"


def test_person_hash_is_salted_and_stable(monkeypatch):
    monkeypatch.setattr(recorder.settings, "jwt_secret_key", "salt-a")
    a = recorder.person_hash("user-1")
    assert a == recorder.person_hash("user-1") and "user-1" not in a
    monkeypatch.setattr(recorder.settings, "jwt_secret_key", "salt-b")
    assert recorder.person_hash("user-1") != a


async def _chat(monkeypatch, mode):
    from app.services import assistant_briefings

    monkeypatch.setattr(assistant_agent.settings, "assistant_agent_v2", True)
    monkeypatch.setattr(assistant_agent.settings, "hybrid_mode", mode)
    monkeypatch.setattr(assistant_briefings, "live_for", AsyncMock(return_value=[]))
    plan = Plan([ToolCall("search_products", {"query": "seeni"})], "want", "L2", "products.want", "want|<P>", language="si")
    monkeypatch.setattr(engine, "plan_for", AsyncMock(return_value=plan))
    monkeypatch.setattr(assistant_agent, "_run_tool", _turn_tool({"seeni": [SUGAR]}))
    agent = AsyncMock(return_value={
        "reply": "සීනි මෙන්න.", "products": [SUGAR], "orders": [], "actions": [], "guide": None, "panels": [],
        "language": "si", "search_context": None, "briefing_note": None, "briefing_ids": [],
        "plan_trace": [{"name": "search_products", "args": {"query": "sugar"}}],
    })
    monkeypatch.setattr(assistant_agent, "run", agent)
    rows = []
    monkeypatch.setattr(recorder, "record", lambda **row: rows.append(row))
    redis = AsyncMock()
    redis.get.return_value = None
    out = await ChatService.chat(AsyncMock(), redis, "guest:x", uuid.uuid4(), "seeni one", lang="si")
    return out, agent, rows, redis


@pytest.mark.asyncio
async def test_active_mode_answers_without_the_model(monkeypatch):
    out, agent, rows, redis = await _chat(monkeypatch, "active")
    assert not agent.called
    assert "ඔව්" in out["reply"] and out["products"][0]["product_id"] == "p-sugar"
    assert "plan_trace" not in out
    assert rows[0]["layer"] == "L2" and rows[0]["plan"]["rule"] == "products.want"
    saved = json.loads(redis.set.call_args.args[1])
    assert saved[-1]["products"][0]["product_id"] == "p-sugar"


@pytest.mark.asyncio
async def test_shadow_mode_lets_the_model_answer_and_records_both_plans(monkeypatch):
    out, agent, rows, _ = await _chat(monkeypatch, "shadow")
    assert agent.called and out["reply"] == "සීනි මෙන්න."
    assert "plan_trace" not in out
    row = rows[0]
    assert row["layer"] == "L4"
    assert row["shadow_plan"]["rule"] == "products.want"
    assert row["model_trace"] == [{"name": "search_products", "args": {"query": "sugar"}}]


@pytest.mark.asyncio
async def test_a_disabled_rule_sends_its_traffic_to_the_model(monkeypatch):
    monkeypatch.setattr(assistant_agent.settings, "hybrid_disabled_rules", "products")
    out, agent, rows, _ = await _chat(monkeypatch, "active")
    assert agent.called and rows[0]["layer"] == "L4"


def test_offline_fast_path_eval_holds(monkeypatch):
    """Every case the fast path answers must be right; it must answer at least half."""
    import asyncio

    from scripts.hybrid_eval import run
    from scripts.run_assistant_evals import harness_targets

    for target, name in harness_targets():  # the harness swaps these; put them back after
        monkeypatch.setattr(target, name, getattr(target, name))

    summary = asyncio.run(run())["summary"]
    assert summary["precision"] == 1.0, summary
    assert summary["coverage"] >= 0.55, summary


# --- agreement between a rule's shadow plan and the model (P2) --------------

from app.services.hybrid.agreement import agree  # noqa: E402


def _plan(*steps):
    return {"rule": "x", "steps": [{"name": n, "args": a} for n, a in steps]}


def test_agreement_judges_what_happened_not_the_bytes():
    rule = _plan(("search_products", {"query": "kiri piti"}))
    assert agree(rule, [{"name": "search_products", "args": {"query": "Kiri Piti 400g"}}]) is True
    assert agree(rule, [{"name": "my_orders", "args": {}}]) is False
    # different words, same products found -> the report passes same_products
    assert agree(_plan(("search_products", {"query": "seeni"})), [{"name": "search_products", "args": {"query": "sugar"}}]) is False
    assert agree(_plan(("search_products", {"query": "seeni"})), [{"name": "search_products", "args": {"query": "sugar"}}], same_products=True) is True


def test_agreement_for_orders_help_and_social():
    assert agree(_plan(("my_orders", {"recent": True})), [{"name": "my_orders", "args": {"recent": True}}]) is True
    assert agree(_plan(("my_orders", {})), [{"name": "my_orders", "args": {"recent": True}}]) is False
    assert agree(_plan(("app_help", {"topic": "wallet"})), [{"name": "app_help", "args": {"topic": "offers"}}]) is False
    assert agree({"rule": "social.greeting", "steps": []}, []) is True
    assert agree({"rule": "social.greeting", "steps": []}, [{"name": "search_products", "args": {}}]) is False
    assert agree(None, []) is None


# --- search first, the model only after it ---------------------------------


def test_a_product_must_match_every_word_for_the_fast_path_to_answer(vocab):
    from app.services.catalog_search_service import covers_all_words, make_plan

    tokens = index_tokens("White Sugar 1kg", "සුදු සීනි 1kg", None, "seeni, sugar")[0]
    assert covers_all_words(tokens, make_plan(vocab, "seeni"))
    assert not covers_all_words(tokens, make_plan(vocab, "parippu seeni"))


@pytest.mark.asyncio
async def test_a_partial_match_goes_to_the_model(monkeypatch):
    from app.services import catalog_search_service

    monkeypatch.setattr(catalog_search_service, "any_covers_all", AsyncMock(return_value=False))
    monkeypatch.setattr(assistant_agent, "_run_tool", _turn_tool({"seeni kiri": [SUGAR]}))
    plan = Plan([ToolCall("search_products", {"query": "seeni kiri"})], "list", "L2", "products.list", "list|<P>", language="si")
    assert await engine.run_plan(plan, db=None, redis=None, user_id=None, branch_id=uuid.uuid4(), history=[], emit=AsyncMock()) is None


@pytest.mark.parametrize(
    "text,query",
    [
        ("seeni one", "seeni"),
        ("mata seeni kilo ekak one", "seeni"),
        ("mokak hari seeni", "seeni"),
        ("mage order eka koheda", None),
        ("seeni epa", None),
        ("cart eka open karanna", None),
        ("SB-10423", None),
    ],
)
def test_rescue_finds_product_words_and_leaves_everything_else(vocab, text, query):
    from app.services.hybrid.rules import rescue_query

    assert rescue_query(parse(text), vocab) == query


@pytest.mark.asyncio
async def test_a_dead_model_still_shows_the_product(monkeypatch, vocab):
    from app.services import assistant_briefings, catalog_search_service
    from app.services.chat_service import GeminiAPIError

    monkeypatch.setattr(assistant_agent.settings, "assistant_agent_v2", True)
    monkeypatch.setattr(assistant_briefings, "live_for", AsyncMock(return_value=[]))
    monkeypatch.setattr(assistant_agent, "_run_tool", _turn_tool({"seeni": [SUGAR]}))
    monkeypatch.setattr(assistant_agent, "run", AsyncMock(side_effect=GeminiAPIError("quota")))
    monkeypatch.setattr(assistant_agent.settings, "hybrid_mode", "off")
    monkeypatch.setattr(recorder, "record", lambda **kwargs: None)
    monkeypatch.setattr(catalog_search_service, "get_vocabulary", AsyncMock(return_value=vocab))
    redis = AsyncMock()
    redis.get.return_value = None
    out = await ChatService.chat(AsyncMock(), redis, "guest:x", uuid.uuid4(), "mokak hari seeni", lang="si")
    assert out["products"][0]["product_id"] == "p-sugar"
    assert not out.get("degraded")
    assert "නමුත්" in out["reply"]


def test_catalogue_is_gzipped_and_private_paths_are_not():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.core.catalog_gzip import CatalogGZipMiddleware

    app = FastAPI()
    app.add_middleware(CatalogGZipMiddleware)
    body = {"x": "y" * 5000}
    app.get("/api/v1/products/search")(lambda: body)
    app.get("/api/v1/users/me")(lambda: body)
    client = TestClient(app)
    assert client.get("/api/v1/products/search").headers.get("content-encoding") == "gzip"
    assert "content-encoding" not in client.get("/api/v1/users/me").headers


# --- the lasting chat log ----------------------------------------------------


@pytest.mark.asyncio
async def test_every_turn_is_logged_with_who_answered(monkeypatch):
    from app.services.hybrid import chat_log

    logged = []
    monkeypatch.setattr(chat_log, "record", lambda **row: logged.append(row))
    out, _, _, _ = await _chat(monkeypatch, "active")
    assert "answered_by" not in out
    assert logged[0]["message"] == "seeni one" and logged[0]["reply"] == out["reply"]
    assert logged[0]["answered_by"] == "L2"
    assert logged[0]["products"][0]["product_id"] == "p-sugar"
