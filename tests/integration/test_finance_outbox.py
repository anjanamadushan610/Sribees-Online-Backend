"""
The finance feed: every money and stock change writes an event in the same
transaction, and Sribees Finance reads them through a locked internal route.
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
from decimal import Decimal

import pytest

from tests.integration.test_order_lifecycle import (  # noqa: F401 -- _webhook_secret is a fixture
    PNG,
    _balance,
    _delivered,
    _dispatched,
    _push,
    _webhook_secret,
)

TOKEN = secrets.token_urlsafe(32)


@pytest.fixture(autouse=True)
def _finance_on(monkeypatch):
    from app.config.settings import settings

    monkeypatch.setattr(settings, "finance_outbox_enabled", True)
    monkeypatch.setattr(settings, "finance_service_token_sha256", hashlib.sha256(TOKEN.encode()).hexdigest())
    monkeypatch.setattr(settings, "finance_allowed_ips", "")


async def _events(api, after: int = 0) -> list[dict]:
    r = await api.get("/internal/finance/events", params={"after": after, "limit": 2000},
                      headers={"Authorization": f"Bearer {TOKEN}"})
    assert r.status_code == 200, r.text
    return r.json()["events"]


def _for(events, order_id, event_type):
    return [e for e in events if e["aggregate_id"] == order_id and e["type"] == event_type]


async def test_the_feed_is_locked(api, monkeypatch):
    from app.config.settings import settings

    assert (await api.get("/internal/finance/events")).status_code == 401
    wrong = await api.get("/internal/finance/events", headers={"Authorization": "Bearer nope"})
    assert wrong.status_code == 401
    edge = await api.get("/internal/finance/events", headers={
        "Authorization": f"Bearer {TOKEN}", "X-Forwarded-For": "203.0.113.9"})
    assert edge.status_code == 403, "a request that came through the edge proxy is refused"
    monkeypatch.setattr(settings, "finance_allowed_ips", "10.99.0.0/16")
    far = await api.get("/internal/finance/events", headers={"Authorization": f"Bearer {TOKEN}"})
    assert far.status_code == 403
    monkeypatch.setattr(settings, "finance_service_token_sha256", "")
    assert (await api.get("/internal/finance/events",
                          headers={"Authorization": f"Bearer {TOKEN}"})).status_code == 404


async def test_an_order_from_placing_to_refund(
    api, world, make_customer, make_product, place_order, make_admin, fake_courier
):
    start = max((e["seq"] for e in await _events(api)), default=0)
    customer, order, manager, product = await _delivered(
        api, make_customer, make_product, place_order, make_admin, fake_courier)
    oid = order["order_id"]

    statuses = [e["payload"]["to_status"] for e in _for(await _events(api, start), oid, "order.status")]
    assert statuses[0] in ("pending", "confirmed")
    # This courier went straight from booked to delivered (no picked_up push):
    # Finance must not depend on seeing a dispatch first.
    assert "ready_for_pickup" in statuses and statuses[-1] == "delivered"

    delivered = _for(await _events(api, start), oid, "order.status")[-1]["payload"]
    assert delivered["from_status"] != "delivered"
    assert delivered["branch"]["branch_id"] == str(world.branch_id)
    assert delivered["amounts"]["total"] == f"{Decimal(str(order['total_amount'])):.2f}"
    [line] = delivered["lines"]
    assert line["product_id"] == str(product) and line["quantity"] == 2

    # A returnless refund: the wallet credit says why, and the refunded snapshot
    # lists what came back.
    r = await api.post(f"/api/v1/orders/{oid}/return/images", files={"file": ("x.png", PNG, "image/png")},
                       headers=customer.headers)
    photo = r.json()["data"]["path"]
    r = await api.post(f"/api/v1/orders/{oid}/return", json={"reason": "Damaged", "images": [photo]},
                       headers=customer.headers)
    assert r.status_code == 200, r.text
    r = await api.post(f"/api/v1/admin/orders/{oid}/return/approve",
                       json={"resolution": "returnless_refund", "note": "Photo shows damage"},
                       headers=manager.headers)
    assert r.status_code == 200, r.text

    events = await _events(api, start)
    [refund] = [e for e in _for(events, oid, "wallet.movement") if e["payload"]["reason"] == "return_refund"]
    assert refund["payload"]["type"] == "refund"
    assert Decimal(refund["payload"]["amount"]) == Decimal(str(_balance(
        await api.get("/api/v1/wallet", headers=customer.headers))))
    refunded = _for(events, oid, "order.status")[-1]["payload"]
    assert refunded["to_status"] == "refunded"
    assert refunded["returned"] == [{"order_item_id": line["order_item_id"], "quantity": 2}]
    if os.environ.get("FINANCE_EVENTS_DUMP"):  # contract sample for Sribees Finance's tests
        with open(os.environ["FINANCE_EVENTS_DUMP"], "w", encoding="utf-8") as fh:
            json.dump([e for e in events if e["aggregate_id"] == oid], fh, indent=1)
    assert len({e["event_id"] for e in events}) == len(events)
    assert [e["seq"] for e in events] == sorted(e["seq"] for e in events)


async def test_wallet_paid_order_cancelled(api, db, world, make_customer, make_product, app_redis, fake_courier):
    from app.config.redis import RedisKeys
    from app.models.wallet import WalletTransactionType
    from app.services.wallet_service import WalletService

    start = max((e["seq"] for e in await _events(api)), default=0)
    customer = await make_customer()
    await WalletService.apply_transaction(db, user_id=customer.user_id, tx_type=WalletTransactionType.REFUND,
                                          amount=Decimal("300"), title="ZZ credit")
    await db.commit()
    product = await make_product(price="250.00")
    await app_redis.setex(RedisKeys.branch_context(str(customer.user_id)), 600,
                          json.dumps({"branch_id": str(world.branch_id), "branch_name": "ZZ"}))
    await api.post("/api/v1/cart/items", json={"product_id": str(product), "quantity": 2, "price": "250",
                                               "name": "ZZ"}, headers=customer.headers)
    r = await api.post("/api/v1/orders", json={
        "delivery_address_id": str(customer.address_id), "contact_phone": customer.phone,
        "alternate_phone": "+94719999999", "payment_method": "COD", "use_wallet": True, "wallet_amount": "200"},
        headers=customer.headers)
    assert r.status_code == 201, r.text
    oid = r.json()["data"]["order_id"]
    r = await api.post(f"/api/v1/orders/{oid}/cancel", headers=customer.headers)
    assert r.status_code == 200, r.text

    moves = [(e["payload"]["reason"], e["payload"]["amount"]) for e in _for(await _events(api, start), oid,
                                                                             "wallet.movement")]
    assert moves == [("wallet_payment", "200.00"), ("cancel_refund", "200.00")]
    # The credit with no reason (a test top-up) is still on the feed, flagged by its null reason.
    loose = [e for e in await _events(api, start) if e["type"] == "wallet.movement" and e["aggregate_id"] is None]
    assert loose and loose[-1]["payload"]["reason"] is None


async def test_nothing_is_written_while_switched_off(api, monkeypatch, make_customer, make_product, place_order):
    from app.config.settings import settings

    start = max((e["seq"] for e in await _events(api)), default=0)
    monkeypatch.setattr(settings, "finance_outbox_enabled", False)
    customer = await make_customer()
    await place_order(customer, await make_product(), 1)
    monkeypatch.setattr(settings, "finance_outbox_enabled", True)
    assert await _events(api, start) == []


async def test_dispatch_snapshot_carries_lines(api, make_customer, make_product, place_order, make_admin,
                                               fake_courier):
    start = max((e["seq"] for e in await _events(api)), default=0)
    _, order, sid, _, product = await _dispatched(api, make_customer, make_product, place_order, make_admin,
                                                  fake_courier, qty=3)
    r, _ = await _push(api, sid, "picked_up")
    assert r.status_code == 200, r.text
    handed = [e["payload"] for e in _for(await _events(api, start), order["order_id"], "order.status")
              if e["payload"]["to_status"] == "handed_to_courier"]
    assert handed and handed[0]["lines"][0]["quantity"] == 3


async def test_daily_totals_for_reconciliation(api, world, make_customer, make_product, place_order,
                                               make_admin, fake_courier):
    from datetime import datetime
    from zoneinfo import ZoneInfo

    today = datetime.now(ZoneInfo("Asia/Colombo")).date().isoformat()
    auth = {"Authorization": f"Bearer {TOKEN}"}

    async def totals():
        r = await api.get("/internal/finance/totals", params={"day": today}, headers=auth)
        assert r.status_code == 200, r.text
        return r.json()["branches"].get(str(world.branch_id), {})

    before = await totals()
    _, order, _, _ = await _delivered(api, make_customer, make_product, place_order, make_admin, fake_courier)
    after = await totals()
    assert after["delivered_count"] == before.get("delivered_count", 0) + 1
    total = Decimal(str(order["total_amount"]))
    assert Decimal(after["delivered_total"]) - Decimal(before.get("delivered_total", "0")) == total
    assert Decimal(after["cod"]) - Decimal(before.get("cod", "0")) == total  # COD, no wallet
    assert (await api.get("/internal/finance/totals", params={"day": today})).status_code == 401


async def test_stock_levels_are_recorded_but_change_nothing_in_the_shop(api, db, world, make_product):
    import uuid as _uuid

    from sqlalchemy import select

    from app.models.finance_outbox import FinanceStockLevel
    from app.models.product import BranchInventory

    product = await make_product(stock=40)
    auth = {"Authorization": f"Bearer {TOKEN}"}

    def level(qty, version):
        return {"levels": [{"branch_id": str(world.branch_id), "product_id": str(product),
                            "sellable_qty": qty, "unit": "each", "version": version}]}

    r = await api.put("/internal/finance/stock-levels", json=level("12", 5), headers=auth)
    assert r.status_code == 200 and r.json() == {"received": 1, "stored": 1, "applied_to_shop": False}
    stale = await api.put("/internal/finance/stock-levels", json=level("99", 4), headers=auth)
    assert stale.json()["stored"] == 0, "an older push never overwrites a newer one"
    assert (await api.put("/internal/finance/stock-levels", json=level("1", 9))).status_code == 401

    row = (await db.execute(select(FinanceStockLevel).where(
        FinanceStockLevel.product_id == _uuid.UUID(str(product))))).scalar_one()
    assert row.sellable_qty == 12 and row.version == 5
    shop = (await db.execute(select(BranchInventory.stock_quantity).where(
        BranchInventory.product_id == _uuid.UUID(str(product)),
        BranchInventory.branch_id == world.branch_id))).scalar_one_or_none()
    assert shop in (None, 40), "the shop still sells from its own stock"
