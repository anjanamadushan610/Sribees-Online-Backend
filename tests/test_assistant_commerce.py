"""Commerce trust boundaries; execute the real Redis Lua CAS in an emulator."""

import copy
import json
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from fakeredis.aioredis import FakeRedis
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy.dialects import postgresql

from app.config.redis import RedisKeys
from app.models.assistant import AssistantReceipt
from app.models.notification import Notification
from app.schemas.assistant_commerce import AssistantPreferences, ConfirmAssistantRequest
from app.schemas.chat import ChatPlan
from app.services import assistant_personal as personal
from app.services import assistant_reminder_worker as worker
from app.services import assistant_shopping as shopping


def product(**kwargs):
    return dict(
        product_id=str(uuid4()),
        name="Milk 500ml",
        pack_name="Milk 500ml",
        price=250,
        discount_price=200,
        is_on_sale=True,
        stock_quantity=5,
        has_variants=False,
        images=[],
        variants=[],
        **kwargs,
    )


def database():
    db = MagicMock()
    db.get = AsyncMock(return_value=None)
    db.execute = AsyncMock(return_value=MagicMock())
    db.commit = AsyncMock()
    return db


@pytest.mark.parametrize(
    "name,expected",
    [
        ("Rice 1kg", (Decimal(1000), "g")),
        ("Milk 2 x 500ml", (Decimal(1000), "ml")),
        ("Oil 1.5 litres", (Decimal(1500), "ml")),
        ("Milk 500ml x 2", None),
        ("Soap", None),
        ("Rice 0g", None),
        ("Tea 100g + 20g", None),
    ],
)
def test_unit_price_requires_unambiguous_net_content(name, expected):
    assert shopping.pack_size(name) == expected


@pytest.mark.parametrize(
    "body",
    [
        {"intent": "cart_edit", "operations": [{"operation": "checkout"}]},
        {"intent": "cart_edit", "operations": [{"operation": "add", "quantity": -1}]},
        {"intent": "memory", "memory_forget_fields": ["password"]},
        {"intent": "reminders", "reminder": {"interval_days": 0}},
    ],
)
def test_model_plan_rejects_unsupported_writes(body):
    with pytest.raises(ValidationError):
        ChatPlan(reply="Preview", **body)


def test_client_cannot_submit_executable_payload():
    with pytest.raises(ValidationError):
        ConfirmAssistantRequest(token=uuid4().hex, user_id=str(uuid4()))
    with pytest.raises(ValidationError):
        AssistantPreferences(budget_lkr=float("nan"))


@pytest.mark.asyncio
async def test_ambiguous_add_never_changes_cart(monkeypatch):
    redis = FakeRedis(decode_responses=True)
    user, branch = uuid4(), uuid4()
    key = RedisKeys.cart(str(user))
    await redis.set(key, '{"items":[],"coupon":null}')
    before = await redis.get(key)
    monkeypatch.setattr(
        shopping, "search", AsyncMock(return_value=[product(), product()])
    )
    result = await shopping.cart_preview(
        None,
        redis,
        user,
        branch,
        {"operations": [{"operation": "add", "query": "milk", "quantity": 1}]},
        "si",
    )
    assert len(result["products"]) == 2
    assert not result.get("panels")
    assert await redis.get(key) == before
    assert not await redis.keys("assistant:proposal:*")
    await redis.aclose()


@pytest.mark.asyncio
async def test_remove_out_of_stock_item_uses_own_basket(monkeypatch):
    item = {
        "product_id": str(uuid4()),
        "name": "Milk",
        "variant_id": None,
        "quantity": 2,
    }
    original = {"items": [item]}
    lookup = AsyncMock()
    monkeypatch.setattr(shopping, "catalog_product", lookup)
    built, _ = await shopping.build_cart(
        None, uuid4(), original, [{"operation": "remove", "query": "milk"}], "si"
    )
    assert built["items"] == []
    assert original["items"] == [item]
    lookup.assert_not_called()


@pytest.mark.asyncio
async def test_variant_requires_explicit_choice_and_total_stock(monkeypatch):
    p = product()
    a, b = str(uuid4()), str(uuid4())
    p.update(
        has_variants=True,
        variants=[
            {
                "variant_id": a,
                "is_active": True,
                "name": "Small",
                "price": 100,
                "stock_quantity": 5,
            }
        ],
    )
    monkeypatch.setattr(shopping, "catalog_product", AsyncMock(return_value=p))
    op = {"operation": "add", "product_id": p["product_id"], "quantity": 3}
    built, choices = await shopping.build_cart(None, uuid4(), {"items": []}, [op], "en")
    assert built is None and choices == [p]
    existing = {
        "items": [{"product_id": p["product_id"], "variant_id": b, "quantity": 3}]
    }
    with pytest.raises(HTTPException, match="STOCK_CHANGED"):
        await shopping.build_cart(
            None, uuid4(), existing, [op | {"variant_id": a}], "en"
        )


async def preview(monkeypatch, redis):
    user, branch, p = uuid4(), uuid4(), product()
    monkeypatch.setattr(
        shopping, "catalog_product", AsyncMock(side_effect=lambda *a: copy.deepcopy(p))
    )
    result = await shopping.cart_preview(
        None,
        redis,
        user,
        branch,
        {
            "operations": [
                {"operation": "add", "product_id": p["product_id"], "quantity": 2}
            ]
        },
        "si",
    )
    token = result["panels"][0]["commands"][0]["token"]
    key = shopping.proposal_key(user, token)
    raw = await redis.get(key)
    return user, branch, p, key, raw, json.loads(raw)["payload"]


@pytest.mark.asyncio
async def test_lua_confirmation_updates_once_and_preserves_totals(monkeypatch):
    redis = FakeRedis(decode_responses=True)
    user, branch, _, key, raw, payload = await preview(monkeypatch, redis)
    assert await redis.get(RedisKeys.cart(str(user))) is None
    reply = await shopping.confirm_cart(
        None, redis, user, branch, key, raw, payload, "si"
    )
    again = await shopping.confirm_cart(
        None, redis, user, branch, key, raw, payload, "si"
    )
    cart = json.loads(await redis.get(RedisKeys.cart(str(user))))
    assert reply == again and reply["cart_changed"] is True
    assert cart["items"][0]["quantity"] == 2
    assert cart["items"][0]["price"] == 200
    assert await redis.ttl(key) > 86000
    await redis.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("drift", ["cart", "price", "stock", "branch", "expired"])
async def test_confirmation_rejects_changed_or_expired_previews(monkeypatch, drift):
    redis = FakeRedis(decode_responses=True)
    user, branch, p, key, raw, payload = await preview(monkeypatch, redis)
    cart_key = RedisKeys.cart(str(user))
    if drift == "cart":
        await redis.set(cart_key, '{"items":[],"coupon":"NEW"}')
    elif drift == "price":
        p["discount_price"] = 210
    elif drift == "stock":
        p["stock_quantity"] = 1
    elif drift == "branch":
        branch = uuid4()
    else:
        await redis.delete(key)
    before = await redis.get(cart_key)
    with pytest.raises(HTTPException) as exc:
        await shopping.confirm_cart(None, redis, user, branch, key, raw, payload, "si")
    assert exc.value.status_code == 409
    assert await redis.get(cart_key) == before
    await redis.aclose()


@pytest.mark.asyncio
async def test_comparison_uses_effective_sale_and_pack_price(monkeypatch):
    p = product()
    monkeypatch.setattr(shopping, "catalog_product", AsyncMock(return_value=p))
    result = await shopping.comparison(
        None,
        uuid4(),
        {"intent": "compare_products", "product_ids": [p["product_id"]]},
        [],
        "en",
    )
    values = [row["value"] for row in result["panels"][0]["sections"][0]["rows"]]
    assert "LKR 200.00" in values
    assert "LKR 40.00 / 100 ml" in values


@pytest.mark.asyncio
async def test_preferences_are_only_proposed_and_selective_forget_preserves_rest():
    db, redis, user = database(), FakeRedis(decode_responses=True), uuid4()
    saved = AssistantPreferences(
        favorite_brands=["Brand A"], household_size=4
    ).model_dump()
    db.get.return_value = SimpleNamespace(preferences=saved)
    result = await personal.memory_result(
        db,
        redis,
        user,
        {"memory_operation": "forget", "memory_forget_fields": ["favorite_brands"]},
        "si",
    )
    token = result["panels"][0]["commands"][0]["token"]
    payload = json.loads(await redis.get(shopping.proposal_key(user, token)))["payload"]
    assert payload["preferences"]["household_size"] == 4
    assert payload["preferences"]["favorite_brands"] == []
    db.add.assert_not_called()
    db.commit.assert_not_called()
    assert await redis.get(shopping.proposal_key(uuid4(), token)) is None
    await redis.aclose()


@pytest.mark.asyncio
async def test_guest_cannot_read_or_save_personal_data():
    db, redis = database(), AsyncMock()
    for result in [
        await personal.memory_result(db, redis, None, {}, "si"),
        await personal.reminders_result(db, redis, None, uuid4(), {}, "si"),
    ]:
        assert result["actions"][0]["target"] == "login"
    db.get.assert_not_called()
    db.execute.assert_not_called()
    redis.set.assert_not_called()


@pytest.mark.asyncio
async def test_memory_confirmation_rejects_stale_preferences():
    db = database()
    db.get.side_effect = [None, SimpleNamespace(preferences={"household_size": 5})]
    with pytest.raises(HTTPException, match="PREFERENCES_CHANGED"):
        await personal.confirm_personal(
            db,
            uuid4(),
            uuid4().hex,
            "memory",
            {"base": AssistantPreferences().model_dump(), "preferences": {}},
            "en",
        )
    db.commit.assert_not_called()


@pytest.mark.asyncio
async def test_receipt_retry_does_not_repeat_database_write():
    db, user = database(), uuid4()
    db.get.return_value = SimpleNamespace(user_id=user, response={"reply": "Done"})
    assert await personal.confirm_personal(
        db, user, uuid4().hex, "memory", {}, "en"
    ) == {"reply": "Done"}
    db.add.assert_not_called()
    db.commit.assert_not_called()


@pytest.mark.asyncio
async def test_reminder_cancel_query_is_owner_scoped():
    db, user, reminder = database(), uuid4(), uuid4()
    db.execute.return_value.scalar_one_or_none.return_value = None
    with pytest.raises(HTTPException, match="REMINDER_NOT_FOUND"):
        await personal.confirm_personal(
            db,
            user,
            uuid4().hex,
            "reminder_cancel",
            {"reminder_id": str(reminder)},
            "en",
        )
    query = db.execute.call_args.args[0].compile(dialect=postgresql.dialect())
    assert user in query.params.values() and reminder in query.params.values()
    assert "FOR UPDATE" in str(query)
    db.commit.assert_not_called()


def test_reminders_use_sri_lanka_nine_am():
    now = datetime(2026, 9, 21, 22, 0, tzinfo=timezone.utc)  # Sep 22 in Sri Lanka
    assert personal.next_morning(7, now) == datetime(
        2026, 9, 29, 3, 30, tzinfo=timezone.utc
    )


@pytest.mark.asyncio
async def test_purchase_suggestions_only_read_delivered_owned_orders(monkeypatch):
    db, redis, user, pid = (
        database(),
        FakeRedis(decode_responses=True),
        uuid4(),
        uuid4(),
    )
    db.execute.return_value.all.return_value = [
        (pid, "Milk", datetime(2026, 9, 20, tzinfo=timezone.utc)),
        (pid, "Milk", datetime(2026, 9, 13, tzinfo=timezone.utc)),
    ]
    monkeypatch.setattr(personal, "catalog_product", AsyncMock(return_value=product()))
    result = await personal.reminders_result(
        db, redis, user, uuid4(), {"reminder": {"operation": "suggest"}}, "si"
    )
    query = db.execute.call_args.args[0].compile(dialect=postgresql.dialect())
    assert user in query.params.values() and "delivered" in query.params.values()
    assert len(result["panels"][0]["commands"]) == 1
    db.add.assert_not_called()
    db.commit.assert_not_called()
    await redis.aclose()


@pytest.mark.asyncio
async def test_pantry_only_searches_missing_ingredients(monkeypatch):
    lookup = AsyncMock(return_value=[])
    monkeypatch.setattr(personal, "search", lookup)
    await personal.pantry_result(
        None,
        uuid4(),
        {
            "pantry_items": ["Rice", "Onion"],
            "recipes": [
                {
                    "title": "Rice",
                    "servings": 2,
                    "ingredients": ["Rice", "Onion", "Egg"],
                    "missing_ingredients": ["rice", "Egg"],
                    "steps": ["Cook thoroughly."],
                }
            ],
        },
        "en",
    )
    assert lookup.call_args.args[2] == ["Egg"]


@pytest.mark.asyncio
async def test_worker_commits_notification_before_best_effort_push(monkeypatch):
    db = database()
    reminder = SimpleNamespace(
        user_id=uuid4(),
        product_id=uuid4(),
        reminder_id=uuid4(),
        product_name="කිරි",
        language="si",
        interval_days=7,
        next_due_at=None,
        last_sent_at=None,
    )
    db.execute.return_value.scalars.return_value.all.return_value = [reminder]

    async def push(*args):
        db.commit.assert_awaited_once()
        raise RuntimeError("Push unavailable")

    monkeypatch.setattr(worker.NotificationService, "send_push_notification", push)
    assert await worker.dispatch_due(db) == 1
    notification = db.add.call_args.args[0]
    assert isinstance(notification, Notification)
    assert notification.user_id == reminder.user_id
    assert notification.data["type"] == "assistant_reminder"
    assert reminder.next_due_at > reminder.last_sent_at
    query = db.execute.call_args_list[0].args[0].compile(dialect=postgresql.dialect())
    assert "SKIP LOCKED" in str(query) and "assistant_reminders.active IS true" in str(
        query
    )


@pytest.mark.asyncio
async def test_memory_confirm_writes_receipt_in_same_commit():
    db, user, token = database(), uuid4(), uuid4().hex
    values = AssistantPreferences(household_size=4).model_dump()
    await personal.confirm_personal(
        db,
        user,
        token,
        "memory",
        {"base": AssistantPreferences().model_dump(), "preferences": values},
        "si",
    )
    receipt = db.add.call_args.args[0]
    assert isinstance(receipt, AssistantReceipt) and receipt.user_id == user
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_confirmation_api_rejects_another_users_token_and_receipt():
    from starlette.requests import Request

    from app.api.v1 import chat

    redis, db, owner = FakeRedis(decode_responses=True), database(), uuid4()
    command = await shopping.proposal(redis, owner, "memory", {}, "Save")
    db.get.return_value = SimpleNamespace(user_id=owner, response={"reply": "Private"})
    with pytest.raises(HTTPException) as exc:
        await chat.confirm_assistant_action.__wrapped__(
            ConfirmAssistantRequest(token=command["token"]),
            Request({"type": "http", "headers": []}),
            db,
            redis,
            SimpleNamespace(user_id=uuid4()),
        )
    assert exc.value.status_code == 409
    db.commit.assert_not_called()
    await redis.aclose()


@pytest.mark.asyncio
async def test_alternatives_use_source_category_and_exclude_source(monkeypatch):
    source, alternative, branch = product(), product(), uuid4()
    lookup = AsyncMock(
        side_effect=lambda db, branch, pid, lang: (
            alternative if pid == alternative["product_id"] else None
        )
    )
    monkeypatch.setattr(shopping, "catalog_product", lookup)
    monkeypatch.setattr(shopping, "search", AsyncMock(return_value=[]))
    category, subcategory = uuid4(), uuid4()
    monkeypatch.setattr(
        shopping.ProductService,
        "get_by_id",
        AsyncMock(
            return_value=SimpleNamespace(
                is_active=True, category_id=category, subcategory_id=subcategory
            )
        ),
    )
    candidates = AsyncMock(
        return_value=(
            [
                {"product": SimpleNamespace(product_id=source["product_id"])},
                {"product": SimpleNamespace(product_id=alternative["product_id"])},
            ],
            2,
        )
    )
    monkeypatch.setattr(shopping.ProductService, "get_all_for_branch", candidates)
    result = await shopping.comparison(
        None,
        branch,
        {"intent": "alternatives", "product_ids": [source["product_id"]]},
        [],
        "si",
    )
    assert result["products"] == [alternative]
    assert candidates.call_args.args[1] == branch
    assert candidates.call_args.kwargs["category_id"] == category
    assert candidates.call_args.kwargs["subcategory_id"] == subcategory


@pytest.mark.asyncio
async def test_enable_reminder_commits_one_opted_in_schedule():
    db, user, pid = database(), uuid4(), uuid4()
    db.execute.return_value.scalars.return_value.first.return_value = None
    await personal.confirm_personal(
        db,
        user,
        uuid4().hex,
        "reminder_enable",
        {"product_id": str(pid), "name": "කිරි", "days": 14, "language": "si"},
        "si",
    )
    reminder = db.add.call_args_list[0].args[0]
    assert reminder.user_id == user and reminder.product_id == pid
    assert reminder.interval_days == 14 and reminder.active
    assert reminder.next_due_at.hour == 3 and reminder.next_due_at.minute == 30
    assert isinstance(db.add.call_args_list[1].args[0], AssistantReceipt)
    db.commit.assert_awaited_once()
