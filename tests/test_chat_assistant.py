"""Assistant trust boundaries, localization, continuity and courier degradation."""

import json
import re
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.schemas.chat import ChatMessageRequest, ChatPlan
from app.services import assistant_tools
from app.services.chat_service import MAX_HISTORY_TURNS, ChatService
from app.services.embedding_service import GeminiAPIError


def rows(values):
    return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: values))


def order(**overrides):
    now = datetime.now(timezone.utc)
    values = {
        "order_id": uuid4(),
        "user_id": uuid4(),
        "order_number": "SRB-20260920-001",
        "status": "shipped",
        "created_at": now - timedelta(days=1),
        "updated_at": now,
        "total_amount": 1250,
        "items": [SimpleNamespace(product_name="Milk", quantity=2)],
        "courier_shipment_id": "SHP-1",
        "courier_tracking_status": "in_transit",
        "courier_tracking_updated_at": now - timedelta(minutes=5),
        "courier_waybill_number": "SHP-1",
        "courier_will_retry": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest.mark.parametrize(
    "body",
    [
        {"message": " "},
        {"task": "refund"},
        {"task": "navigate", "target": "https://evil.example"},
        {"message": "hi", "image_base64": "invalid", "image_mime_type": "image/jpeg"},
        {"message": "hi", "image_mime_type": "image/jpeg"},
        {"message": "hi", "image_base64": "SGVsbG8=", "image_mime_type": "image/png"},
        {"message": "my orders", "user_id": str(uuid4())},
    ],
)
def test_reject_invalid_requests(body):
    with pytest.raises(ValidationError):
        ChatMessageRequest(**body)


def test_photo_only_and_shortcuts_are_valid():
    import base64

    request = ChatMessageRequest(
        image_base64=base64.b64encode(b"\xff\xd8\xffimage").decode(),
        image_mime_type="image/jpeg",
    )
    assert request.message == ""
    assert ChatMessageRequest(task="recent_orders").task == "recent_orders"


@pytest.mark.parametrize(
    "text",
    [
        "not JSON",
        "[]",
        '{"reply":"hello","intent":"execute_sql"}',
        '{"reply":"yes","intent":"buy_product","max_price":NaN}',
        '{"reply":"yes","intent":"navigate","target":"admin"}',
    ],
)
def test_invalid_model_output_is_never_displayed_or_executed(text):
    with pytest.raises(GeminiAPIError):
        ChatService._parse_reply(text)


@pytest.mark.asyncio
async def test_guest_cannot_read_orders_even_with_known_reference():
    db = AsyncMock()
    result = await assistant_tools.orders_result(db, None, str(uuid4()), "si")
    db.execute.assert_not_called()
    assert result["actions"][0]["target"] == "login"
    assert "පිවිසෙන්න" in result["reply"]


@pytest.mark.asyncio
@pytest.mark.parametrize("reference", [str(uuid4()), "SOMEONE-ELSES-ORDER"])
async def test_every_order_lookup_enforces_authenticated_owner(reference):
    user_id = uuid4()
    db = AsyncMock()
    db.execute.return_value = rows([])
    result = await assistant_tools.orders_result(db, user_id, reference, "en")
    query = db.execute.call_args.args[0]
    params = query.compile().params
    assert params["user_id_1"] == user_id
    assert "orders.user_id =" in str(query)
    assert "orders" not in result
    assert "couldn't find" in result["reply"]


@pytest.mark.asyncio
async def test_courier_outage_returns_honest_saved_card(monkeypatch):
    item = order()
    db = AsyncMock()
    db.execute.side_effect = [rows([item]), rows([])]
    refresh = AsyncMock(return_value=None)
    monkeypatch.setattr(
        assistant_tools.courier_sync_service, "refresh_order_tracking", refresh
    )
    result = await assistant_tools.orders_result(db, item.user_id, None, "si")
    card = result["orders"][0]
    assert card["freshness"] == "unavailable"
    assert card["status"] == "shipped"
    assert card["action"]["order_id"] == str(item.order_id)
    assert card["timeline"][-1]["state"] == "upcoming"
    assert "delivery_verification_code" not in card
    refresh.assert_awaited_once()


@pytest.mark.asyncio
async def test_recent_courier_status_does_not_trigger_duplicate_lookup(monkeypatch):
    item = order(courier_tracking_updated_at=datetime.now(timezone.utc))
    db = AsyncMock()
    db.execute.side_effect = [rows([item]), rows([])]
    refresh = AsyncMock()
    monkeypatch.setattr(
        assistant_tools.courier_sync_service, "refresh_order_tracking", refresh
    )
    result = await assistant_tools.orders_result(db, item.user_id, None, "en")
    assert result["orders"][0]["freshness"] == "recent"
    refresh.assert_not_called()


@pytest.mark.asyncio
async def test_courier_refresh_updates_canvas_from_real_order(monkeypatch):
    item = order()

    async def refresh(db, actual):
        assert actual is item
        actual.status = "delivered"
        actual.courier_tracking_status = "delivered"
        return {"status": "delivered"}

    monkeypatch.setattr(
        assistant_tools.courier_sync_service, "refresh_order_tracking", refresh
    )
    db = AsyncMock()
    db.execute.side_effect = [rows([item]), rows([])]
    result = await assistant_tools.orders_result(db, item.user_id, None, "si")
    card = result["orders"][0]
    assert card["freshness"] == "live"
    assert card["status_label"] == "බෙදාහැර අවසන්"
    assert not any(m["state"] == "upcoming" for m in card["timeline"])


@pytest.mark.asyncio
async def test_order_history_is_bounded_and_does_not_poll_couriers(monkeypatch):
    item = order()
    db = AsyncMock()
    db.execute.side_effect = [rows([item]), rows([])]
    refresh = AsyncMock()
    monkeypatch.setattr(
        assistant_tools.courier_sync_service, "refresh_order_tracking", refresh
    )
    result = await assistant_tools.orders_result(
        db, item.user_id, None, "si", recent=True
    )
    assert db.execute.call_args_list[0].args[0].compile().params["param_1"] == 3
    assert result["orders"][0]["freshness"] == "saved"
    refresh.assert_not_called()


def test_failed_delivery_never_marks_delivery_complete():
    item = order(
        status="delivery_failed",
        courier_tracking_status="failed",
        courier_will_retry=True,
    )
    event = SimpleNamespace(new_status="delivery_failed", created_at=item.updated_at)
    card = assistant_tools.order_card(item, [event], "si", "live")
    assert card["timeline"][-1]["state"] == "upcoming"
    assert card["will_retry"] is True
    assert card["status_label"] == "බෙදාහැරීමේ උත්සාහය අසාර්ථකයි"


@pytest.mark.parametrize("lang,phrase", [("si", "හමු වුණේ නැහැ"), ("ta", "கிடைக்கவில்லை")])
def test_no_match_replies_obey_application_language(lang, phrase):
    reply = ChatService._ground_reply(
        "We have everything", "buy_product", ["milk"], None, None, [], [], lang
    )
    assert phrase in reply
    assert "We have everything" not in reply


@pytest.mark.asyncio
async def test_missing_branch_is_not_reported_as_out_of_stock():
    plan = ChatPlan(
        reply="Yes", intent="buy_product", search_terms=["milk"]
    ).model_dump()
    result = await ChatService._product_response(AsyncMock(), None, plan, [], "si")
    assert result["actions"][0]["target"] == "addresses"
    assert not result["products"]


@pytest.mark.asyncio
async def test_sinhala_mode_does_not_emit_singlish_chitchat():
    plan = ChatPlan(reply="Ow, kiyanna!", intent="chitchat").model_dump()
    result = await ChatService._product_response(AsyncMock(), None, plan, [], "si")
    assert "උදව්" in result["reply"]


@pytest.mark.asyncio
async def test_shortcuts_work_without_model_and_restore_rich_history(monkeypatch):
    redis = AsyncMock()
    redis.get.return_value = None
    ask = AsyncMock(side_effect=GeminiAPIError("offline"))
    monkeypatch.setattr(ChatService, "_ask_gemini", ask)
    result = await ChatService.chat(
        AsyncMock(),
        redis,
        "guest:one",
        None,
        "",
        lang="si",
        task="app_help",
        target="checkout",
    )
    ask.assert_not_called()
    assert len(result["guide"]["steps"]) == 4
    stored = json.loads(redis.set.call_args.args[1])
    assert stored[-1]["guide"] == result["guide"]
    assert stored[-1]["actions"] == result["actions"]


@pytest.mark.asyncio
async def test_turn_reports_its_stages_and_still_runs_without_a_listener(
    monkeypatch,
):
    """
    The SSE endpoint's only job is to relay these. A turn must report the
    same stages it has always gone through, and must behave identically when
    nobody is listening (the plain POST passes no callback).
    """
    plan = ChatPlan(
        reply="Checking", intent="buy_product", search_terms=["milk"]
    ).model_dump()
    monkeypatch.setattr(ChatService, "_ask_gemini", AsyncMock(return_value=plan))
    monkeypatch.setattr(
        ChatService, "_match_products", AsyncMock(return_value=([], []))
    )

    seen = []

    async def on_stage(stage, detail):
        seen.append((stage, detail))

    redis = AsyncMock()
    redis.get.return_value = None
    streamed = await ChatService.chat(
        AsyncMock(),
        redis,
        "guest:one",
        uuid4(),
        "milk",
        lang="si",
        on_stage=on_stage,
    )
    assert [stage for stage, _ in seen] == ["understanding", "searching"]
    assert seen[1][1]["terms"] == ["milk"]

    redis_plain = AsyncMock()
    redis_plain.get.return_value = None
    plain = await ChatService.chat(
        AsyncMock(), redis_plain, "guest:one", uuid4(), "milk", lang="si"
    )
    assert plain["reply"] == streamed["reply"]


@pytest.mark.asyncio
async def test_the_cost_settings_actually_reach_gemini(monkeypatch):
    """
    Thinking tokens are billed as output, and every stored turn is prompt
    tokens paid for again on the next message. Both are configuration, which
    is exactly the kind of thing that silently stops being wired up.
    """
    import httpx

    from app.config.settings import settings
    from app.services import chat_service as chat_module

    captured = {}

    class _Response:
        status_code = 200

        @staticmethod
        def json():
            return {
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {
                                    "text": ChatPlan(
                                        reply="ok", intent="chitchat"
                                    ).model_dump_json()
                                }
                            ]
                        }
                    }
                ],
                "usageMetadata": {"promptTokenCount": 10},
            }

    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, json=None, headers=None):
            captured["payload"] = json
            return _Response()

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    monkeypatch.setattr(settings, "gemini_api_key", "test-key")
    monkeypatch.setattr(settings, "gemini_chat_thinking_budget", 0)

    history = [{"role": "user", "text": f"turn {i}"} for i in range(60)]
    await chat_module.ChatService._ask_gemini("hello", history, None, None)

    config = captured["payload"]["generationConfig"]
    assert config["thinkingConfig"]["thinkingBudget"] == 0
    # The prompt must not carry the whole conversation back every time.
    assert (
        len(captured["payload"]["contents"])
        == chat_module.MAX_HISTORY_TURNS * 2 + 1
    )
    assert chat_module.MAX_HISTORY_TURNS == settings.gemini_chat_history_turns
    # The system prompt is the cacheable prefix; it must lead, identically
    # for every caller, or implicit caching has nothing to match on.
    system = captured["payload"]["systemInstruction"]["parts"][0]["text"]
    assert system.startswith(chat_module._SYSTEM_PROMPT)


@pytest.mark.asyncio
@pytest.mark.asyncio
async def test_model_outage_has_localized_fallback(monkeypatch):
    redis = AsyncMock()
    redis.get.return_value = None
    monkeypatch.setattr(
        ChatService, "_ask_gemini", AsyncMock(side_effect=GeminiAPIError("offline"))
    )
    result = await ChatService.chat(
        AsyncMock(), redis, "guest:one", None, "hello", lang="si"
    )
    assert result["degraded"]
    # The fallback used to point at a shortcut row under the composer;
    # that row is gone, so it must not promise buttons any more.
    assert "බොත්තම්" not in result["reply"]
    assert re.search(r"[\u0d80-\u0dff]", result["reply"])
    assert not result["products"]


@pytest.mark.asyncio
async def test_history_does_not_accept_non_turn_json():
    redis = AsyncMock()
    redis.get.return_value = '{"role":"user"}'
    assert await ChatService.get_history(redis, "guest:a") == []
    redis.get.return_value = json.dumps(
        [None, 123] + [{"role": "user", "text": "hello"}] * 100
    )
    assert len(await ChatService.get_history(redis, "guest:a")) == MAX_HISTORY_TURNS * 2


def test_unknown_help_target_cannot_create_arbitrary_navigation():
    result = assistant_tools.help_result("javascript:alert(1)", "si")
    assert result["actions"][0]["target"] == "support"


@pytest.mark.asyncio
async def test_gemini_receives_language_as_system_instruction(monkeypatch):
    from app.services import chat_service

    monkeypatch.setattr(chat_service.settings, "gemini_api_key", "test-key")
    response = SimpleNamespace(
        status_code=200,
        json=lambda: {
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {"thought": True, "text": "hidden reasoning"},
                            {"text": '{"reply":"ආයුබෝවන්","intent":"chitchat"}'},
                        ]
                    }
                }
            ]
        },
    )
    client = AsyncMock()
    client.__aenter__.return_value = client
    client.post.return_value = response
    monkeypatch.setattr(chat_service.httpx, "AsyncClient", lambda **kwargs: client)
    result = await ChatService._ask_gemini("hello", [], None, None, lang="si")
    request = client.post.call_args.kwargs
    assert "Sinhala Unicode" in request["json"]["systemInstruction"]["parts"][0]["text"]
    assert request["headers"] == {"x-goog-api-key": "test-key"}
    assert "params" not in request
    assert result["reply"] == "ආයුබෝවන්"


@pytest.mark.asyncio
async def test_sinhala_catalog_matches_survive_relevance_filter(monkeypatch):
    from app.api.v1 import products

    product = SimpleNamespace(
        product_id=uuid4(),
        name="Fresh milk",
        name_si="නැවුම් කිරි",
        name_ta=None,
        category=None,
        subcategory=None,
    )
    monkeypatch.setattr(
        products, "format_product", lambda product, **kwargs: {"name": product.name_si}
    )
    from app.services.product_service import ProductService

    monkeypatch.setattr(
        ProductService,
        "get_all_for_branch",
        AsyncMock(return_value=([{"product": product, "effective": {}}], 1)),
    )
    strict, _ = await ChatService._match_products(
        AsyncMock(), uuid4(), ["කිරි"], lang="si"
    )
    assert strict == [{"name": "නැවුම් කිරි"}]


@pytest.mark.asyncio
async def test_api_passes_authenticated_identity_and_full_canvas(monkeypatch):
    from starlette.requests import Request

    from app.api.v1 import chat

    user_id = uuid4()
    redis = AsyncMock()
    redis.set.return_value = True
    request = Request({"type": "http", "headers": [(b"accept-language", b"si-LK")]})
    monkeypatch.setattr(chat, "_resolve_branch_id", AsyncMock(return_value=None))
    expected = {
        "reply": "මෙන්න",
        "products": [],
        "orders": [{"order_number": "SRB-1"}],
        "guide": None,
        "actions": [],
        "degraded": False,
    }
    chat_call = AsyncMock(return_value=expected)
    monkeypatch.setattr(ChatService, "chat", chat_call)
    result = await chat.send_chat_message.__wrapped__(
        ChatMessageRequest(task="recent_orders"),
        request,
        AsyncMock(),
        redis,
        SimpleNamespace(user_id=user_id),
    )
    assert result["data"] == expected
    assert chat_call.call_args.kwargs["user_id"] == user_id
    assert chat_call.call_args.kwargs["lang"] == "si"
    redis.eval.assert_awaited_once()


@pytest.mark.asyncio
async def test_busy_session_cannot_clear_in_flight_history():
    from fastapi import HTTPException
    from starlette.requests import Request

    from app.api.v1 import chat

    redis = AsyncMock()
    redis.set.return_value = False
    request = Request({"type": "http", "headers": [(b"x-device-id", b"test-device")]})
    with pytest.raises(HTTPException) as exc:
        await chat.clear_chat_history(request, redis, None)
    assert exc.value.status_code == 409
    redis.delete.assert_not_called()


@pytest.mark.asyncio
async def test_history_marks_a_preview_the_customer_already_confirmed():
    """
    Reopening the chat used to offer every past confirm button again, because
    only the app's in-memory set knew they had been pressed.
    """
    from starlette.requests import Request

    from app.api.v1 import chat

    done_token = "a" * 32
    open_token = "b" * 32
    redis = AsyncMock()
    redis.get.return_value = json.dumps(
        [
            {
                "role": "assistant",
                "text": "Preview",
                "panels": [
                    {
                        "title": "Basket preview",
                        "commands": [
                            {"token": done_token, "label": "Confirm"},
                            {"token": open_token, "label": "Confirm"},
                        ],
                    }
                ],
            }
        ]
    )
    redis.mget.return_value = [json.dumps({"done": {"reply": "Saved"}}), None]
    db = AsyncMock()
    db.execute.return_value = []
    user = SimpleNamespace(user_id=uuid4())
    request = Request({"type": "http", "headers": []})
    result = await chat.get_chat_history(request, db, redis, user)
    commands = result["data"]["history"][0]["panels"][0]["commands"]
    assert commands[0]["confirmed"] is True
    assert commands[1]["confirmed"] is False


@pytest.mark.asyncio
async def test_history_endpoint_marks_old_order_data_as_saved():
    from starlette.requests import Request

    from app.api.v1 import chat

    redis = AsyncMock()
    redis.get.return_value = json.dumps(
        [
            {
                "role": "assistant",
                "text": "Order",
                "search_context": {"private": True},
                "orders": [{"freshness": "live"}],
            }
        ]
    )
    request = Request({"type": "http", "headers": [(b"x-device-id", b"test-device")]})
    result = await chat.get_chat_history(request, AsyncMock(), redis, None)
    turn = result["data"]["history"][0]
    assert "search_context" not in turn
    assert turn["orders"][0]["freshness"] == "saved"
