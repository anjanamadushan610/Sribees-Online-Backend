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


async def test_opening_snapshot_for_go_live(api, db, world, make_customer, make_product, place_order,
                                            make_admin, fake_courier, app_redis):
    from app.config.redis import RedisKeys
    from app.models.wallet import WalletTransactionType
    from app.services.wallet_service import WalletService

    auth = {"Authorization": f"Bearer {TOKEN}"}
    # One order paid partly from the wallet and still open; one delivered.
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
    open_id = r.json()["data"]["order_id"]
    _, done, _, _ = await _delivered(api, make_customer, make_product, place_order, make_admin, fake_courier)

    r = await api.get("/internal/finance/opening-snapshot", params={"cutoff": "2026-01-01"}, headers=auth)
    assert r.status_code == 200, r.text
    snap = r.json()
    assert snap["seq"] == max(e["seq"] for e in await _events(api))
    assert all(n <= snap["seq"] for n in snap["missing"])
    assert all(c["seq"] > snap["missing"][0] for c in snap["counted_after_gap"])
    assert str(world.branch_id) in {b["branch_id"] for b in snap["branches"]}
    orders = {o["order_id"]: o for o in snap["orders"]}
    assert orders[open_id]["advance"] == "200.00"
    assert orders[open_id]["delivered"] is False and orders[open_id]["dispatched"] is False
    assert orders[open_id]["branch_id"] == str(world.branch_id)
    assert orders[done["order_id"]]["delivered"] is True and orders[done["order_id"]]["dispatched"] is True
    assert Decimal(snap["customer_wallets"]["total"]) >= Decimal("100.00")  # this customer's 300 - 200

    # Orders before the cut-off are tests: left out, and only counted when still open.
    later = (await api.get("/internal/finance/opening-snapshot", params={"cutoff": "2999-01-01"},
                           headers=auth)).json()
    assert later["orders"] == [] and later["open_orders_before_cutoff"] >= 1
    assert (await api.get("/internal/finance/opening-snapshot",
                          params={"cutoff": "2026-01-01"})).status_code == 401


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


async def test_stock_compare_for_the_parallel_run(api, world, make_customer, make_product, place_order):
    auth = {"Authorization": f"Bearer {TOKEN}"}
    product = await make_product(stock=40)
    await place_order(await make_customer(), product, 3)
    r = await api.put("/internal/finance/stock-levels", headers=auth, json={"levels": [{
        "branch_id": str(world.branch_id), "product_id": str(product), "sellable_qty": "40",
        "unit": "each", "version": 1}]})
    assert r.status_code == 200, r.text

    r = await api.get("/internal/finance/stock-compare", headers=auth)
    assert r.status_code == 200, r.text
    rows = {(x["branch_id"], x["product_id"]): x for x in r.json()["rows"]}
    mine = rows[(str(world.branch_id), str(product))]
    # Placed, not dispatched: Online has taken 3 off already, Finance has not.
    assert mine["committed"] == "3"
    assert mine["finance_qty"] == "40.000" and mine["finance_unit"] == "each"
    assert mine["name"]
    assert (await api.get("/internal/finance/stock-compare")).status_code == 401


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


async def test_catalog_carries_branch_prices_rates_and_weight(api, db, world, make_product):
    """Sribees Finance's minimum price needs what each item sells for in each branch
    (before and after Quick Sale), the cashback and referral it carries, and its
    weight -- worked out by checkout's own functions."""
    import uuid as _uuid

    from app.config.settings import settings
    from app.models.product import Product, ProductVariant

    rice = await make_product(price="250.00", discount=10, branch_price="260.00")
    sugar = await make_product(price="300.00")
    product = await db.get(Product, sugar)
    product.stock_mode = "packed"
    half = ProductVariant(product_id=sugar, name="500g", sku=f"ZZ500-{_uuid.uuid4().hex[:6]}",
                          price=Decimal("150.00"), weight_grams=500, is_active=True)
    db.add(half)
    await db.commit()

    r = await api.get("/internal/finance/catalog", headers={"Authorization": f"Bearer {TOKEN}"})
    assert r.status_code == 200, r.text
    rows = {p["product_id"]: p for p in r.json()["products"]}
    mine = rows[str(rice)]
    assert mine["weight_kg"] == "1.000"
    [here] = [b for b in mine["branch_prices"] if b["branch_id"] == str(world.branch_id)]
    platform = f"{Decimal(str(settings.cashback_rate)) * 100:.2f}"
    assert here == {"branch_id": str(world.branch_id), "price": "260.00", "effective_price": "234.00",
                    "discount_pct": "10.00", "cashback_pct": platform, "referral_pct": "0.00"}
    # A pre-packed product is priced per pack size, on the variant.
    packed = rows[str(sugar)]
    assert packed["branch_prices"] == []
    [opt] = [v for v in packed["variants"] if v["variant_id"] == str(half.variant_id)]
    [pack_here] = [b for b in opt["branch_prices"] if b["branch_id"] == str(world.branch_id)]
    assert (pack_here["price"], pack_here["effective_price"]) == ("150.00", "150.00")


async def test_min_prices_are_stored_newest_first_and_shown_to_the_branch(
    api, db, world, make_product, make_admin
):
    rice = await make_product(price="250.00")
    auth = {"Authorization": f"Bearer {TOKEN}"}

    def push(price, version, **extra):
        return {"prices": [{"branch_id": str(world.branch_id), "product_id": str(rice),
                            "min_price": price, "ready_cost": "228.66", "margin_pct": "10.00",
                            "cashback_pct": "0.50", "referral_pct": "0.50",
                            "cost_ref": "RPK-000002 (2026-10-17)", "version": version, **extra}]}

    r = await api.put("/internal/finance/min-prices", json=push("257.00", 3), headers=auth)
    assert r.status_code == 200 and r.json() == {"received": 1, "stored": 1}
    stale = await api.put("/internal/finance/min-prices", json=push("999.00", 2), headers=auth)
    assert stale.json()["stored"] == 0, "an older push never overwrites a newer one"
    assert (await api.put("/internal/finance/min-prices", json=push("1.00", 9))).status_code == 401

    manager = await make_admin("branch_manager", api=api)
    hdr = {"Authorization": f"Bearer {manager.token}"}
    r = await api.get("/api/v1/admin/inventory/price-floors", headers=hdr,
                      params={"product_ids": str(rice)})
    assert r.status_code == 200, r.text
    [floor] = r.json()["data"]["floors"][str(rice)]
    assert floor["min_price"] == "257.00" and floor["ready_cost"] == "228.66"
    assert floor["cost_ref"] == "RPK-000002 (2026-10-17)"
    # Advice only: the shop's price is untouched.
    from app.models.product import Product

    assert (await db.get(Product, rice)).price == Decimal("250.00")
    bad = await api.get("/api/v1/admin/inventory/price-floors", headers=hdr,
                        params={"product_ids": "not-an-id"})
    assert bad.status_code == 400
