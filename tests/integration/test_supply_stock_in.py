"""
Goods received in Sribees Supply reach the shop's stock: added once, never set, in the
product's own measure, with a hidden row for a branch that does not carry it.
"""
from __future__ import annotations

import hashlib
import secrets
import uuid
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.models.product import (
    BranchInventory,
    BranchVariantInventory,
    Product,
    ProductVariant,
)
from app.services import inventory_batches

TOKEN = secrets.token_urlsafe(32)
AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture(autouse=True)
def _on(monkeypatch):
    from app.config.settings import settings

    monkeypatch.setattr(settings, "supply_service_token_sha256", hashlib.sha256(TOKEN.encode()).hexdigest())
    monkeypatch.setattr(settings, "supply_allowed_ips", "")
    monkeypatch.setattr(settings, "supply_stock_in_enabled", True)
    monkeypatch.setattr(settings, "finance_stock_authority", False)


def _line(product, units, **extra):
    return {"line_id": str(uuid.uuid4()), "product_id": str(product), "units": units, **extra}


async def _send(api, branch, *lines):
    r = await api.post("/internal/supply/receipts", headers=AUTH, json={
        "branch_id": str(branch), "receipt_no": "RCV-000001", "lines": list(lines)})
    assert r.status_code == 200, r.text
    return r.json()["results"]


async def _row(db, branch, product):
    db.expire_all()
    return await db.scalar(select(BranchInventory).where(
        BranchInventory.branch_id == branch, BranchInventory.product_id == product))


async def test_the_route_is_off_until_switched_on(api, world, make_product, monkeypatch):
    from app.config.settings import settings

    branch = world.branch_id
    product = await make_product(stock=5)
    body = {"branch_id": str(branch), "receipt_no": "R", "lines": [_line(product, 1)]}
    assert (await api.post("/internal/supply/receipts", json=body)).status_code == 401
    monkeypatch.setattr(settings, "supply_stock_in_enabled", False)
    assert (await api.post("/internal/supply/receipts", headers=AUTH, json=body)).status_code == 404
    monkeypatch.setattr(settings, "supply_stock_in_enabled", True)
    monkeypatch.setattr(settings, "finance_stock_authority", True)
    assert (await api.post("/internal/supply/receipts", headers=AUTH, json=body)).status_code == 409


async def test_units_are_added_once(api, db, world, make_product):
    branch = world.branch_id
    product = await make_product(stock=30)
    line = _line(product, 60, purchase_cost="210.00")
    assert (await _send(api, branch, line))[0]["status"] == "added"
    assert (await _row(db, branch, product)).stock_quantity == 90
    # the same line again (a retry) adds nothing
    assert (await _send(api, branch, line))[0]["status"] == "already"
    row = await _row(db, branch, product)
    assert row.stock_quantity == 90
    assert await inventory_batches.list_batches(db, row.inventory_id) == []


async def test_a_new_price_makes_batches(api, db, world, make_product):
    branch = world.branch_id
    product = await make_product(price="250.00", stock=10)
    r = await _send(api, branch, _line(product, 5, listed_price="300.00", selling_price="280.00",
                                       purchase_cost="210.00"))
    assert r[0]["status"] == "added", r
    row = await _row(db, branch, product)
    got = await inventory_batches.list_batches(db, row.inventory_id)
    assert row.stock_quantity == 15
    assert [(b.lot_code, b.remaining, b.selling_price) for b in got] == [
        ("Opening stock", 10, Decimal("250.00")), ("RCV-000001", 5, Decimal("280.00"))]
    # once there are batches, the next delivery is a batch at the newest price
    await _send(api, branch, _line(product, 2))
    row = await _row(db, branch, product)
    got = await inventory_batches.list_batches(db, row.inventory_id)
    assert row.stock_quantity == 17
    assert (got[-1].remaining, got[-1].selling_price, got[-1].listed_price) == (
        2, Decimal("280.00"), Decimal("300.00"))


async def test_a_price_above_the_listed_one_changes_nothing(api, db, world, make_product):
    branch = world.branch_id
    product = await make_product(price="250.00", stock=10)
    line = _line(product, 5, listed_price="200.00", selling_price="280.00")
    r = await _send(api, branch, line)
    assert r[0]["status"] == "refused"
    assert (await _row(db, branch, product)).stock_quantity == 10
    # refused lines are not remembered: the corrected line goes in
    r = await _send(api, branch, {**line, "listed_price": "300.00"})
    assert r[0]["status"] == "added", r


async def test_a_branch_without_the_product_gets_a_hidden_row(api, db, world, make_product):
    branch = world.branch_id
    product = await make_product(stock=0)
    await db.execute(BranchInventory.__table__.delete().where(
        BranchInventory.product_id == product))
    await db.commit()
    assert (await _send(api, branch, _line(product, 4)))[0]["status"] == "added"
    row = await _row(db, branch, product)
    assert (row.stock_quantity, row.is_active) == (4, False)


async def test_loose_and_packed_goods(api, db, world, make_product):
    branch = world.branch_id
    loose, packed = await make_product(stock=0), await make_product(stock=0)
    (await db.get(Product, loose)).stock_mode = "loose"
    (await db.get(Product, packed)).stock_mode = "packed"
    kilo = ProductVariant(variant_id=uuid.uuid4(), product_id=loose, name="1 kg", weight_grams=1000,
                          price=Decimal("400.00"))
    pack = ProductVariant(variant_id=uuid.uuid4(), product_id=packed, name="500 g", weight_grams=500,
                          price=Decimal("200.00"))
    db.add_all([kilo, pack])
    await db.commit()
    kilo_id, pack_id = kilo.variant_id, pack.variant_id

    r = await _send(api, branch, _line(loose, 1, grams=25_000), _line(packed, 6, variant_id=str(pack_id)),
                    _line(loose, 2, variant_id=str(kilo_id)))
    assert [x["status"] for x in r] == ["added"] * 3, r
    assert (await _row(db, branch, loose)).stock_grams == 27_000
    assert (await db.scalar(select(BranchVariantInventory.stock_quantity).where(
        BranchVariantInventory.variant_id == pack_id))) == 6
    assert (await _row(db, branch, packed)).stock_quantity == 6

    r = await _send(api, branch, _line(loose, 1), _line(packed, 1),
                    _line(packed, 1, variant_id=str(kilo_id)), _line(uuid.uuid4(), 1))
    assert [x["status"] for x in r] == ["refused"] * 4, r
    assert (await _row(db, branch, loose)).stock_grams == 27_000


async def test_stock_info_and_weight(api, db, world, make_product):
    branch = world.branch_id
    product = await make_product(price="250.00", stock=7)
    row = await db.get(Product, product)
    row.weight, row.weight_unit = Decimal("0.100"), "kg"
    await db.commit()
    r = await api.post("/internal/supply/stock-info", headers=AUTH, json={
        "branch_id": str(branch), "items": [{"product_id": str(product)}]})
    assert r.status_code == 200, r.text
    item = r.json()["items"][0]
    assert (item["stock_quantity"], item["selling_price"], item["has_batches"]) == (7, "250.00", False)
    catalog = (await api.get("/internal/supply/catalog", headers=AUTH)).json()
    assert next(p for p in catalog["products"] if p["product_id"] == str(product))["weight_grams"] == 100


@pytest.mark.parametrize("weight,unit,grams", [
    ("1.5", "kg", 1500), ("250", "g", 250), ("1", "L", 1000), ("330", "ml", 330),
    ("2", None, 2000), (None, "kg", None), ("0", "kg", None), ("3", "boxes", None)])
def test_weight_in_grams(weight, unit, grams):
    from app.api.supply_internal import weight_grams

    assert weight_grams(Decimal(weight) if weight else None, unit) == grams


async def test_stock_levels_carry_min_and_max(api, db, world, make_product):
    from fastapi import HTTPException

    from app.api.v1.admin_inventory import check_max

    branch = world.branch_id
    product = await make_product(stock=7)
    row = await _row(db, branch, product)
    row.low_stock_threshold, row.max_stock_level, row.reserved_quantity = 10, 40, 2
    await db.commit()
    r = await api.get("/internal/supply/stock-levels", headers=AUTH, params={"branch_id": str(branch)})
    assert r.status_code == 200, r.text
    item = next(i for i in r.json()["items"] if i["product_id"] == str(product))
    assert (item["stock_quantity"], item["reserved_quantity"], item["low_stock_threshold"],
            item["max_stock_level"]) == (7, 2, 10, 40)
    assert (await api.get("/internal/supply/stock-levels",
                          params={"branch_id": str(branch)})).status_code == 401
    check_max(10, None)
    check_max(10, 11)
    with pytest.raises(HTTPException):
        check_max(10, 10)
