"""The tool-using assistant loop, with Gemini replaced by scripted responses."""

import json
import uuid
from unittest.mock import AsyncMock

import pytest

from app.services import assistant_agent
from app.services.chat_service import ChatService

BRANCH = uuid.uuid4()
USER = uuid.uuid4()


def product(pid, name, price):
    return {"product_id": pid, "name": name, "price": price, "discount_price": None, "is_on_sale": False, "variants": []}


SUGAR = product("p-sugar", "සුදු සීනි 1kg", 260)
DHAL = product("p-dhal", "පරිප්පු 1kg", 390)


def model_turn(*calls, signature=True):
    parts = []
    for n, (name, args) in enumerate(calls):
        part = {"functionCall": {"name": name, "args": args, "id": f"call_{name}_{n}"}}
        if signature and n == 0:
            part["thoughtSignature"] = "sig-abc"
        parts.append(part)
    return {"candidates": [{"content": {"role": "model", "parts": parts}}]}


class Script:
    """Feeds scripted model turns and records every payload sent."""

    def __init__(self, *turns):
        self.turns = list(turns)
        self.payloads = []

    async def __call__(self, payload):
        self.payloads.append(json.loads(json.dumps(payload)))
        return self.turns.pop(0)


async def run(monkeypatch, script, message="seeni saha parippu one, mage order eka koheda", **kw):
    monkeypatch.setattr(ChatService, "_generate", staticmethod(script))
    return await assistant_agent.run(
        db=AsyncMock(),
        redis=AsyncMock(),
        user_id=kw.get("user_id", USER),
        branch_id=kw.get("branch_id", BRANCH),
        message=message,
        image_base64=None,
        image_mime_type=None,
        history=[],
        fixed_lang="si",
        fallback_lang="si",
        application_context=None,
        briefing_prompt="",
        emit=AsyncMock(),
    )


def fake_search(catalog):
    async def _match(db, branch_id, terms, **kwargs):
        hits = [p for p in catalog if terms[0] in p["_q"]]
        return hits, hits

    return _match


CATALOG = [{**SUGAR, "_q": "seeni sugar"}, {**DHAL, "_q": "parippu dhal"}]


@pytest.mark.asyncio
async def test_several_requests_in_one_message(monkeypatch):
    monkeypatch.setattr(ChatService, "_match_products", staticmethod(fake_search(CATALOG)))
    orders = AsyncMock(return_value={"reply": "ඔබේ ඇණවුම", "orders": [{"order_number": "SB-1001", "status_label": "පිටත්ව ඇත"}]})
    monkeypatch.setattr(assistant_agent, "orders_result", orders)
    script = Script(
        model_turn(("search_products", {"query": "seeni"}), ("search_products", {"query": "parippu"}), ("my_orders", {})),
        model_turn(("respond", {"text": "සීනි රු. 260යි, පරිප්පු රු. 390යි. ඔබේ ඇණවුම SB-1001 පිටත්ව ඇත.", "show_product_ids": ["p-dhal", "p-sugar"]})),
    )
    out = await run(monkeypatch, script)
    assert out["reply"].startswith("සීනි රු. 260යි")
    assert [p["product_id"] for p in out["products"]] == ["p-dhal", "p-sugar"]
    assert out["orders"][0]["order_number"] == "SB-1001"
    assert out["language"] == "si"
    # The model's turn went back exactly as received, signature included.
    echoed = script.payloads[1]["contents"][-2]
    assert echoed["parts"][0]["thoughtSignature"] == "sig-abc"
    replies = script.payloads[1]["contents"][-1]["parts"]
    assert [r["functionResponse"]["id"] for r in replies] == ["call_search_products_0", "call_search_products_1", "call_my_orders_2"]


@pytest.mark.asyncio
async def test_invented_price_falls_back_to_server_copy(monkeypatch):
    monkeypatch.setattr(ChatService, "_match_products", staticmethod(fake_search(CATALOG)))
    script = Script(
        model_turn(("search_products", {"query": "seeni"})),
        model_turn(("respond", {"text": "සීනි රු. 199යි!", "show_product_ids": ["p-sugar"]})),
    )
    out = await run(monkeypatch, script, message="seeni kiyada")
    assert "199" not in out["reply"]
    assert out["reply"].startswith("ගැළපෙන භාණ්ඩ මෙන්න")
    assert [p["product_id"] for p in out["products"]] == ["p-sugar"]


@pytest.mark.asyncio
async def test_ids_no_tool_returned_are_never_shown(monkeypatch):
    monkeypatch.setattr(ChatService, "_match_products", staticmethod(fake_search(CATALOG)))
    script = Script(
        model_turn(("search_products", {"query": "seeni"})),
        model_turn(("respond", {"text": "සීනි වර්ග මෙන්න.", "show_product_ids": ["p-made-up", "p-sugar"]})),
    )
    out = await run(monkeypatch, script, message="seeni")
    assert [p["product_id"] for p in out["products"]] == ["p-sugar"]


@pytest.mark.asyncio
async def test_empty_search_tells_the_model_to_try_again(monkeypatch):
    monkeypatch.setattr(ChatService, "_match_products", staticmethod(fake_search(CATALOG)))
    script = Script(
        model_turn(("search_products", {"query": "kiri piti"})),
        model_turn(("search_products", {"query": "sugar"})),
        model_turn(("respond", {"text": "සීනි වර්ග මෙන්න.", "show_product_ids": ["p-sugar"]})),
    )
    out = await run(monkeypatch, script, message="seeni")
    first = script.payloads[1]["contents"][-1]["parts"][0]["functionResponse"]["response"]
    assert first["products"] == [] and "Try once more" in first["note"]
    assert out["products"][0]["product_id"] == "p-sugar"


@pytest.mark.asyncio
async def test_bad_arguments_come_back_as_instructions(monkeypatch):
    script = Script(
        model_turn(("stage_basket_change", {"operations": [{"operation": "add", "quantity": 500}]})),
        model_turn(("respond", {"text": "කරුණාකර ප්‍රමාණය කියන්න."})),
    )
    out = await run(monkeypatch, script, message="kiri 500k danna")
    error = script.payloads[1]["contents"][-1]["parts"][0]["functionResponse"]["response"]["error"]
    assert "Invalid arguments" in error and "stage_basket_change" in error
    assert out["reply"] == "කරුණාකර ප්‍රමාණය කියන්න."


@pytest.mark.asyncio
async def test_last_round_may_only_respond(monkeypatch):
    monkeypatch.setattr(ChatService, "_match_products", staticmethod(fake_search(CATALOG)))
    monkeypatch.setattr(assistant_agent.settings, "assistant_agent_max_rounds", 2)
    script = Script(
        model_turn(("search_products", {"query": "seeni"})),
        model_turn(("respond", {"text": "සීනි වර්ග මෙන්න.", "show_product_ids": ["p-sugar"]})),
    )
    await run(monkeypatch, script, message="seeni")
    assert "allowedFunctionNames" not in script.payloads[0]["toolConfig"]["functionCallingConfig"]
    assert script.payloads[1]["toolConfig"]["functionCallingConfig"]["allowedFunctionNames"] == ["respond"]


@pytest.mark.asyncio
async def test_no_branch_asks_for_a_location(monkeypatch):
    script = Script(
        model_turn(("search_products", {"query": "seeni"})),
        model_turn(("respond", {"text": "මුලින් ඔබේ ස්ථානය තෝරන්න."})),
    )
    out = await run(monkeypatch, script, message="seeni", branch_id=None)
    assert out["actions"][0]["target"] == "addresses"
    assert not out["products"]


@pytest.mark.asyncio
async def test_delivery_time_promise_is_replaced(monkeypatch):
    script = Script(model_turn(("respond", {"text": "විනාඩි 30කින් ගෙනත් දෙනවා!"})))
    out = await run(monkeypatch, script, message="ikmanata genath denna")
    assert "විනාඩි" not in out["reply"]


@pytest.mark.asyncio
async def test_a_failing_tool_does_not_sink_the_turn(monkeypatch):
    monkeypatch.setattr(assistant_agent, "orders_result", AsyncMock(side_effect=RuntimeError("db down")))
    script = Script(
        model_turn(("my_orders", {})),
        model_turn(("respond", {"text": "දැනට ඇණවුම බලන්න බැහැ, සහාය සේවාවෙන් උදව් ගන්න."})),
    )
    out = await run(monkeypatch, script, message="mage order eka koheda")
    error = script.payloads[1]["contents"][-1]["parts"][0]["functionResponse"]["response"]["error"]
    assert "failed" in error
    assert out["reply"].startswith("දැනට ඇණවුම")


@pytest.mark.asyncio
async def test_customers_own_numbers_may_be_repeated(monkeypatch):
    monkeypatch.setattr(ChatService, "_match_products", staticmethod(fake_search(CATALOG)))
    script = Script(
        model_turn(("search_products", {"query": "seeni", "max_price": 300})),
        model_turn(("respond", {"text": "රු. 300ට අඩු සීනි මෙන්න.", "show_product_ids": ["p-sugar"]})),
    )
    out = await run(monkeypatch, script, message="rupiyal 300ta adu seeni")
    assert out["reply"] == "රු. 300ට අඩු සීනි මෙන්න."


@pytest.mark.asyncio
async def test_cards_follow_the_language_the_model_settled_on(monkeypatch):
    """Romanised text: tools run in the fallback language, the reply ends up English."""
    from app.services import assistant_shopping

    monkeypatch.setattr(ChatService, "_match_products", staticmethod(fake_search(CATALOG)))
    english = {**SUGAR, "name": "White Sugar 1kg"}
    monkeypatch.setattr(assistant_shopping, "catalog_product", AsyncMock(return_value=english))
    script = Script(
        model_turn(("search_products", {"query": "seeni"})),
        model_turn(("respond", {"text": "Here is the sugar.", "show_product_ids": ["p-sugar"], "reply_language": "en"})),
    )
    monkeypatch.setattr(ChatService, "_generate", staticmethod(script))
    out = await assistant_agent.run(
        db=AsyncMock(), redis=AsyncMock(), user_id=USER, branch_id=BRANCH, message="sugar please",
        image_base64=None, image_mime_type=None, history=[], fixed_lang=None, fallback_lang="si",
        application_context=None, briefing_prompt="", emit=AsyncMock(),
    )
    assert out["language"] == "en"
    assert out["products"][0]["name"] == "White Sugar 1kg"


@pytest.mark.asyncio
async def test_generate_retries_without_a_thinking_budget_the_model_refuses(monkeypatch):
    import httpx

    sent = []

    async def post(self, url, json=None, headers=None):
        sent.append(json)
        if "thinkingConfig" in json["generationConfig"]:
            return httpx.Response(400, json={"error": {"message": "Request contains an invalid argument."}})
        return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": "{}"}]}}]})

    monkeypatch.setattr(httpx.AsyncClient, "post", post)
    monkeypatch.setattr(assistant_agent.settings, "gemini_api_key", "test-key")
    data = await ChatService._generate(
        {"contents": [], "generationConfig": {"temperature": 0.3, "thinkingConfig": {"thinkingBudget": 0}}}
    )
    assert data["candidates"]
    assert len(sent) == 2 and "thinkingConfig" not in sent[1]["generationConfig"]


@pytest.mark.asyncio
async def test_chat_uses_the_agent_when_switched_on(monkeypatch):
    from app.services import assistant_briefings

    monkeypatch.setattr(assistant_agent.settings, "assistant_agent_v2", True)
    monkeypatch.setattr(assistant_briefings, "live_for", AsyncMock(return_value=[]))
    monkeypatch.setattr(ChatService, "_match_products", staticmethod(fake_search(CATALOG)))
    script = Script(
        model_turn(("search_products", {"query": "seeni"})),
        model_turn(("respond", {"text": "සීනි වර්ග මෙන්න.", "show_product_ids": ["p-sugar"]})),
    )
    monkeypatch.setattr(ChatService, "_generate", staticmethod(script))
    redis = AsyncMock()
    redis.get.return_value = None
    out = await ChatService.chat(AsyncMock(), redis, "guest:x", BRANCH, "seeni one", lang="si")
    assert out["reply"] == "සීනි වර්ග මෙන්න."
    assert out["products"][0]["product_id"] == "p-sugar"
    assert out["language"] == "si" and "briefing_note" not in out
    saved = json.loads(redis.set.call_args.args[1])
    assert saved[-1]["search_context"]["search_terms"] == ["seeni"]
    assert saved[-1]["products"][0]["product_id"] == "p-sugar"


def test_tool_schemas_are_plain_json():
    tools = assistant_agent.tool_declarations()[0]["functionDeclarations"]
    assert {t["name"] for t in tools} >= {"search_products", "my_orders", "stage_basket_change", "respond"}
    json.dumps(tools)
