"""Account tools (roadmap P4): own data only, sign-in required, writes as previews."""

import json
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fakeredis.aioredis import FakeRedis

from app.services import assistant_account as acc
from app.services import assistant_agent

USER = uuid.uuid4()
BRANCH = uuid.uuid4()
SUGAR = {"product_id": str(uuid.uuid4()), "name": "White Sugar 1kg", "price": 260, "discount_price": None, "is_on_sale": False, "variants": []}


def ctx(user_id=USER, redis=None):
    return {"db": AsyncMock(), "redis": redis or FakeRedis(), "user_id": user_id, "branch_id": BRANCH, "history": [], "emit": AsyncMock()}


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ["wallet_and_points", "my_wishlist", "my_offers", "stage_reorder", "my_referrals", "my_notifications"])
async def test_guests_are_asked_to_sign_in(tool):
    turn = assistant_agent.Turn("en")
    out = await assistant_agent._run_tool(tool, {}, ctx(user_id=None), turn)
    assert out["signed_in"] is False
    assert turn.actions[0]["target"] == "login"


@pytest.mark.asyncio
async def test_wallet_summary_reads_the_customers_own_numbers(monkeypatch):
    from app.services.loyalty_service import LoyaltyService
    from app.services.order_reward_service import OrderRewardService
    from app.services.wallet_service import WalletService

    seen = {}

    async def balance(db, user_id):
        seen["user"] = user_id
        return 450

    monkeypatch.setattr(WalletService, "get_balance", staticmethod(balance))
    monkeypatch.setattr(OrderRewardService, "pending_for_user", staticmethod(AsyncMock(return_value=[])))
    monkeypatch.setattr(OrderRewardService, "pending_total", staticmethod(lambda rows: 0))
    monkeypatch.setattr(
        LoyaltyService, "summary",
        staticmethod(AsyncMock(return_value={"points": 120, "tier": {"name": "Silver"}, "next_tier": {"name": "Gold", "min_points": 200}})),
    )
    turn = assistant_agent.Turn("si")
    out = await assistant_agent._run_tool("wallet_and_points", {}, ctx(), turn)
    assert seen["user"] == USER
    assert out["wallet_balance"] == 450 and out["points_to_next_level"] == 80
    assert "රු. 450" in out["server_message"]
    assert turn.panels and turn.actions[0]["target"] == "wallet"


@pytest.mark.asyncio
async def test_wishlist_add_is_only_a_preview_with_a_token(monkeypatch):
    monkeypatch.setattr(acc, "catalog_product", AsyncMock(return_value=SUGAR))
    redis = FakeRedis()
    turn = assistant_agent.Turn("en")
    out = await assistant_agent._run_tool("stage_wishlist_add", {"product_id": SUGAR["product_id"]}, ctx(redis=redis), turn)
    assert out["needs_confirmation"] is True
    token = turn.panels[0]["commands"][0]["token"]
    stored = json.loads(await redis.get(f"assistant:proposal:{USER}:{token}"))
    assert stored == {"kind": "wishlist_add", "payload": {"product_id": SUGAR["product_id"], "price": 260.0}}


@pytest.mark.asyncio
async def test_wishlist_add_refuses_an_unknown_product(monkeypatch):
    monkeypatch.setattr(acc, "catalog_product", AsyncMock(return_value=None))
    turn = assistant_agent.Turn("en")
    out = await assistant_agent._run_tool("stage_wishlist_add", {"product_id": "nope"}, ctx(), turn)
    assert "Unknown product_id" in out["error"] and not turn.panels


@pytest.mark.asyncio
async def test_confirming_a_wishlist_preview_adds_the_item_with_its_receipt():
    from app.models.assistant import AssistantReceipt
    from app.models.wishlist import WishlistItem
    from app.services.assistant_personal import confirm_personal

    added = []
    db = AsyncMock()
    db.add = lambda obj: added.append(obj)
    db.get.return_value = None
    db.execute.return_value = SimpleNamespace(scalar_one_or_none=lambda: None)
    token = uuid.uuid4().hex
    await confirm_personal(db, USER, token, "wishlist_add", {"product_id": SUGAR["product_id"], "price": 260}, "en")
    kinds = [type(o) for o in added]
    assert WishlistItem in kinds and AssistantReceipt in kinds
    item = next(o for o in added if isinstance(o, WishlistItem))
    assert item.user_id == USER and str(item.product_id) == SUGAR["product_id"]
    db.commit.assert_awaited()


@pytest.mark.asyncio
async def test_reorder_previews_only_in_stock_lines(monkeypatch):
    from app.services.order_service import OrderService

    order = SimpleNamespace(order_id=uuid.uuid4(), order_number="SB-1001")
    lines = [
        {"product_id": SUGAR["product_id"], "variant_id": None, "name": "White Sugar 1kg", "quantity": 2, "available": True},
        {"product_id": str(uuid.uuid4()), "variant_id": None, "name": "Old Biscuit", "quantity": 1, "available": False},
    ]
    monkeypatch.setattr(OrderService, "reorder_preview", staticmethod(AsyncMock(return_value=lines)))
    captured = {}

    async def preview(db, redis, user_id, branch_id, plan, lang):
        captured.update(plan)
        return {"reply": "Check the preview below and confirm it to apply.", "panels": [{"title": "Basket", "commands": [{"token": "t"}]}]}

    monkeypatch.setattr(acc, "cart_preview", preview)
    db = AsyncMock()
    db.execute.return_value = SimpleNamespace(scalar_one_or_none=lambda: order)
    out = await acc.reorder(db, FakeRedis(), USER, BRANCH, "sb-1001", "en")
    assert [op["product_id"] for op in captured["operations"]] == [SUGAR["product_id"]]
    assert captured["operations"][0]["quantity"] == 2
    assert out["data"]["unavailable"] == ["Old Biscuit"] and out["data"]["needs_confirmation"]


def test_new_tools_are_declared_to_the_model():
    names = {t["name"] for t in assistant_agent.tool_declarations()[0]["functionDeclarations"]}
    assert assistant_agent.ACCOUNT_TOOLS <= names
