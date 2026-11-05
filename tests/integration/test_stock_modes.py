"""
How a branch keeps stock of a weight product (migration 100, app/services/stock_keeping.py):
loose = grams on branch_inventory, packed = a count per weight option. And Sribees
Finance setting the shop's stock once both halves of the switch are on.
"""
from __future__ import annotations

import hashlib
import secrets
import uuid

import pytest
from sqlalchemy import select

from app.services import stock_keeping
from tests.integration.test_admin_catalog import _category, _id
from tests.integration.test_weight_options import _inventory_id, _options, _weighed

TOKEN = secrets.token_urlsafe(32)


async def _set_mode(api, boss, product, mode):
    r = await api.put(f"/api/v1/admin/products/{product}", json={"stock_mode": mode},
                      headers=boss.headers)
    assert r.status_code == 200, r.text
    assert r.json()["data"]["stock_mode"] == mode


async def _shelf(db, world, product) -> tuple[int, int]:
    """(grams, the figure screens read)"""
    from app.models.product import BranchInventory

    db.expire_all()
    return tuple((await db.execute(select(BranchInventory.stock_grams, BranchInventory.stock_quantity).where(
        BranchInventory.branch_id == world.branch_id,
        BranchInventory.product_id == uuid.UUID(str(product))))).one())


async def test_loose_goods_are_kept_in_grams(api, db, world, make_admin, make_product):
    boss = await make_admin("super_admin", api=api)
    product = await _weighed(api, boss, make_product)
    await _inventory_id(db, world, product)
    options = await _options(api, boss, product)
    half, kilo = options["500g"]["variant_id"], options["1kg"]["variant_id"]

    await _set_mode(api, boss, product, "loose")
    assert await _shelf(db, world, product) == (0, 0), "the old figure was packs; it is zeroed"
    item = stock_keeping.Item("loose", uuid.UUID(str(product)), None, None)
    assert await stock_keeping.set_level(db, world.branch_id, item, 2000)
    await db.commit()

    assert await stock_keeping.available(db, world.branch_id, product, options["250g"]["variant_id"]) == 8
    # Screens and the app read packs of the smallest weight: 2,000 g = 8 x 250 g.
    assert await _shelf(db, world, product) == (2000, 8)
    await stock_keeping.take(db, world.branch_id, product, half, 3)          # 1,500 g
    await db.commit()
    assert await _shelf(db, world, product) == (500, 2)
    with pytest.raises(stock_keeping.OutOfStock) as short:
        await stock_keeping.take(db, world.branch_id, product, kilo, 1)
    assert short.value.left == 0
    await db.rollback()
    await stock_keeping.give_back(db, world.branch_id, product, half, 1)   # a cancel
    await db.commit()
    assert await _shelf(db, world, product) == (1000, 4)


async def test_packed_goods_count_each_size(api, db, world, make_admin, make_product):
    boss = await make_admin("super_admin", api=api)
    product = await _weighed(api, boss, make_product)
    await _inventory_id(db, world, product)
    options = await _options(api, boss, product)
    small, kilo = options["250g"]["variant_id"], options["1kg"]["variant_id"]

    await _set_mode(api, boss, product, "packed")
    small_item = stock_keeping.Item("packed", uuid.UUID(str(product)), uuid.UUID(small), 250)
    assert await stock_keeping.set_level(db, world.branch_id, small_item, 5)
    await db.commit()

    assert await stock_keeping.available(db, world.branch_id, product, small) == 5
    assert await stock_keeping.available(db, world.branch_id, product, kilo) == 0
    assert (await _shelf(db, world, product))[1] == 5, "all packs together, so the app lists it"
    await stock_keeping.take(db, world.branch_id, product, small, 2)
    await db.commit()
    assert await stock_keeping.available(db, world.branch_id, product, small) == 3
    assert (await _shelf(db, world, product))[1] == 3
    with pytest.raises(stock_keeping.OutOfStock):
        await stock_keeping.take(db, world.branch_id, product, kilo, 1)
    await db.rollback()

    # An old line without a size (ordered before the product was packed) is
    # cancelled: the summary stays the sum of the sizes, nothing is invented.
    await stock_keeping.give_back(db, world.branch_id, product, None, 3)
    await db.commit()
    assert (await _shelf(db, world, product))[1] == 3
    assert await stock_keeping.available(db, world.branch_id, product, small) == 3


async def test_only_weight_products_can_be_loose_or_packed(api, make_admin, make_product):
    boss = await make_admin("super_admin", api=api)
    soap = await make_product()
    cat = _id(await _category(api, boss), "category_id", "id")
    for mode in ("loose", "packed"):
        r = await api.put(f"/api/v1/admin/products/{soap}", json={"stock_mode": mode, "category_id": cat},
                          headers=boss.headers)
        assert r.status_code == 409 and "sold by weight" in r.json()["detail"], r.text


async def test_unit_goods_and_untracked_products_are_as_before(db, world, make_product):
    product = await make_product(stock=10)
    left = await stock_keeping.available(db, world.branch_id, product)
    assert left is None or left == 10
    assert await stock_keeping.available(db, uuid.uuid4(), product) is None


async def test_finance_sets_the_shop_only_with_both_halves_on(
    api, db, world, make_admin, make_product, monkeypatch
):
    from app.config.settings import settings

    monkeypatch.setattr(settings, "finance_service_token_sha256", hashlib.sha256(TOKEN.encode()).hexdigest())
    monkeypatch.setattr(settings, "finance_allowed_ips", "")
    auth = {"Authorization": f"Bearer {TOKEN}"}
    boss = await make_admin("super_admin", api=api)
    product = await _weighed(api, boss, make_product)
    await _inventory_id(db, world, product)
    await _set_mode(api, boss, product, "loose")
    version = [1]

    async def push(authority: bool, kg: str) -> dict:
        version[0] += 1
        r = await api.put("/internal/finance/stock-levels", headers=auth, json={"authority": authority, "levels": [{
            "branch_id": str(world.branch_id), "product_id": str(product), "sellable_qty": kg,
            "unit": "kg", "version": version[0]}]})
        assert r.status_code == 200, r.text
        return r.json()

    monkeypatch.setattr(settings, "finance_stock_authority", False)
    assert (await push(True, "3.5"))["applied_to_shop"] == 0, "Online's half is off"
    monkeypatch.setattr(settings, "finance_stock_authority", True)
    assert (await push(False, "3.5"))["applied_to_shop"] == 0, "the owner's half is off"
    assert (await push(True, "3.5"))["applied_to_shop"] == 1
    assert (await _shelf(db, world, product))[0] == 3500

    # Under authority the product's stock mode cannot be changed in Online.
    r = await api.put(f"/api/v1/admin/products/{product}", json={"stock_mode": "unit"},
                      headers=boss.headers)
    assert r.status_code == 409
    # A push in the wrong measure for the product is never applied.
    version[0] += 1
    r = await api.put("/internal/finance/stock-levels", headers=auth, json={"authority": True, "levels": [{
        "branch_id": str(world.branch_id), "product_id": str(product), "sellable_qty": "7",
        "unit": "each", "version": version[0]}]})
    assert r.json()["applied_to_shop"] == 0
    assert (await _shelf(db, world, product))[0] == 3500


async def test_branch_counts_weights_and_the_app_sees_each_size(
    api, db, app_redis, world, make_admin, make_product, make_customer
):
    from tests.integration.test_weight_options import _branch_context

    boss = await make_admin("super_admin", api=api)
    manager = await make_admin("branch_manager", api=api)
    product = await _weighed(api, boss, make_product)
    inventory_id = await _inventory_id(db, world, product)
    options = await _options(api, boss, product)
    await _set_mode(api, boss, product, "packed")

    # The old stock figure is refused for a weight product; the weights screen counts it.
    r = await api.put(f"/api/v1/admin/inventory/{inventory_id}", json={"stock_quantity": 9},
                      headers=manager.headers)
    assert r.status_code == 409, r.text
    # The Quick Edit form resends the unchanged figure with a new price: that is fine.
    r = await api.put(f"/api/v1/admin/inventory/{inventory_id}",
                      json={"stock_quantity": 0, "branch_price": "990.00"}, headers=manager.headers)
    assert r.status_code == 200, r.text
    r = await api.put(f"/api/v1/admin/inventory/{inventory_id}/weights", headers=manager.headers, json={
        "packs": [{"variant_id": options["250g"]["variant_id"], "stock_quantity": 6},
                  {"variant_id": options["1kg"]["variant_id"], "stock_quantity": 0}]})
    assert r.status_code == 200, r.text
    counted = {o["label"]: o for o in r.json()["data"]["options"]}
    assert counted["250g"]["packs"] == 6 and counted["1kg"]["available"] == 0
    assert r.json()["data"]["stock_quantity"] == 6

    customer = await make_customer()
    await _branch_context(app_redis, world, customer)
    r = await api.get(f"/api/v1/products/{product}", headers=customer.headers)
    assert r.status_code == 200, r.text
    shown = {o["label"]: o["available"] for o in r.json()["data"]["weight_options"]}
    assert shown["250g"] == 6 and shown["1kg"] == 0
    assert r.json()["data"]["stock_quantity"] == 6

    # Loose: grams.
    await _set_mode(api, boss, product, "loose")
    r = await api.put(f"/api/v1/admin/inventory/{inventory_id}/weights", headers=manager.headers,
                      json={"stock_grams": 1200})
    assert r.status_code == 200, r.text
    counted = {o["label"]: o["available"] for o in r.json()["data"]["options"]}
    assert counted == {"250g": 4, "500g": 2, "1kg": 1, "2kg": 0, "5kg": 0}


async def test_finance_push_never_adds_back_what_is_ordered_or_unbooked(
    api, db, world, make_customer, make_product, place_order, monkeypatch
):
    """Review 2026-11-05: the shop's figure = Finance's sellable - orders placed and not yet
    sent - orders sent after the last event Finance had booked. Read under the row lock."""
    from sqlalchemy import update

    from app.config.settings import settings
    from app.models.finance_outbox import FinanceOutbox
    from app.models.order import Order

    monkeypatch.setattr(settings, "finance_service_token_sha256", hashlib.sha256(TOKEN.encode()).hexdigest())
    monkeypatch.setattr(settings, "finance_allowed_ips", "")
    monkeypatch.setattr(settings, "finance_stock_authority", True)
    auth = {"Authorization": f"Bearer {TOKEN}"}
    product = await make_product(stock=20)
    order = await place_order(await make_customer(), product, 2)
    version = [10]

    async def push(qty: str, booked_through=None) -> None:
        version[0] += 1
        body = {"authority": True, "levels": [{
            "branch_id": str(world.branch_id), "product_id": str(product), "sellable_qty": qty,
            "unit": "each", "version": version[0]}]}
        if booked_through is not None:
            body["booked_through"] = booked_through
        r = await api.put("/internal/finance/stock-levels", headers=auth, json=body)
        assert r.status_code == 200 and r.json()["applied_to_shop"] == 1, r.text

    await push("10")
    assert (await _shelf(db, world, product))[1] == 8, "the 2 ordered stay off the shelf"

    # The order goes to the courier; Finance has not booked that yet.
    oid = uuid.UUID(str(order.get("order_id") or order["id"]))
    await db.execute(update(Order).where(Order.order_id == oid).values(status="handed_to_courier"))
    ev = FinanceOutbox(event_type="order.status", aggregate_id=oid,
                       payload={"to_status": "handed_to_courier"})
    db.add(ev)
    await db.flush()
    seq = ev.seq
    await db.commit()
    await push("10", booked_through=seq - 1)
    assert (await _shelf(db, world, product))[1] == 8, "sent but not booked: still off the shelf"
    # Finance books the dispatch: its sellable is 8 now, and nothing is off twice.
    await push("8", booked_through=seq)
    assert (await _shelf(db, world, product))[1] == 8


async def test_online_transfers_wait_while_finance_keeps_the_stock(monkeypatch):
    from types import SimpleNamespace

    from app.config.settings import settings
    from app.services.stock_transfer_service import (
        StockTransferError,
        StockTransferService,
    )

    monkeypatch.setattr(settings, "finance_stock_authority", True)
    for step in (StockTransferService.approve, StockTransferService.complete):
        status = "pending" if step is StockTransferService.approve else "in_transit"
        with pytest.raises(StockTransferError, match="Sribees Finance"):
            await step(None, SimpleNamespace(status=status), *([uuid.uuid4()] if status == "pending" else []))
