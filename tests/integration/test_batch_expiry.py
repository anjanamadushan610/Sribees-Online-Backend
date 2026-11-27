"""
Batches and expiry (owner plan 2026-11-27): every Supply delivery with an expiry date is a
batch; a new MRP sells at today's discount; an expired batch is never sold and the sweep
takes it off stock; staff review a batch's price; the expiry list follows alert days.
"""
from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.models.product import BranchInventory, InventoryBatch
from app.services import batch_expiry, inventory_batches
from app.services.supply_stock_in import same_discount

TOKEN = secrets.token_urlsafe(32)
AUTH = {"Authorization": f"Bearer {TOKEN}"}
TODAY = batch_expiry.colombo_today()


@pytest.fixture(autouse=True)
def _on(monkeypatch):
    from app.config.settings import settings

    monkeypatch.setattr(settings, "supply_service_token_sha256", hashlib.sha256(TOKEN.encode()).hexdigest())
    monkeypatch.setattr(settings, "supply_allowed_ips", "")
    monkeypatch.setattr(settings, "supply_stock_in_enabled", True)
    monkeypatch.setattr(settings, "finance_stock_authority", False)


def _line(product, units, **extra):
    return {"line_id": str(uuid.uuid4()), "product_id": str(product), "units": units, **extra}


async def _send(api, branch, *lines, receipt_no="RCV-EXP-1"):
    r = await api.post("/internal/supply/receipts", headers=AUTH, json={
        "branch_id": str(branch), "receipt_no": receipt_no, "lines": list(lines)})
    assert r.status_code == 200, r.text
    return r.json()["results"]


async def _batches(db, branch, product):
    db.expire_all()
    row = await db.scalar(select(BranchInventory).where(
        BranchInventory.branch_id == branch, BranchInventory.product_id == product))
    return row, list(await inventory_batches.list_batches(db, row.inventory_id))


def test_same_discount():
    assert same_discount(Decimal("200"), Decimal("180"), Decimal("220")) == Decimal("198.00")
    assert same_discount(Decimal("165"), Decimal("150"), Decimal("175")) == Decimal("159.00")  # 159.09
    assert same_discount(Decimal("200"), Decimal("200"), Decimal("220")) == Decimal("220")
    assert same_discount(Decimal("0"), Decimal("0"), Decimal("90")) == Decimal("90")


async def test_a_dated_delivery_is_a_batch_and_a_new_mrp_keeps_the_discount(
        api, db, world, make_product):
    branch = world.branch_id
    product = await make_product(price="200.00", stock=10, discount=10)  # sells at 180
    exp = str(TODAY + timedelta(days=90))
    r = await _send(api, branch, _line(product, 5, expires_on=exp, mrp="200.00"))
    assert r[0]["status"] == "added", r
    row, got = await _batches(db, branch, product)
    # same MRP: still a batch, because the delivery carries its expiry date
    assert [(b.lot_code, b.remaining, b.expires_on) for b in got] == [
        ("Opening stock", 10, None), ("RCV-EXP-1", 5, date.fromisoformat(exp))]
    assert got[-1].selling_price == Decimal("180.00")

    r = await _send(api, branch, _line(product, 4, expires_on=exp, mrp="220.00"),
                    receipt_no="RCV-EXP-2")
    assert (r[0]["listed_price"], r[0]["selling_price"]) == ("220.00", "198.00")
    assert (r[0]["old_listed_price"], r[0]["old_selling_price"]) == ("200.00", "180.00")
    row, got = await _batches(db, branch, product)
    assert row.stock_quantity == 19
    # old stock first, at its own price; the new MRP's batch after it
    priced = await inventory_batches.price_lines(
        db, branch, [{"product_id": str(product), "quantity": 19, "name": "x"}])
    assert [(p["quantity"], p["price"]) for p in priced] == [
        (10, Decimal("180.00")), (5, Decimal("180.00")), (4, Decimal("198.00"))]


async def test_an_expired_batch_is_never_sold_and_the_sweep_takes_it_off(
        api, db, world, make_product):
    branch = world.branch_id
    product = await make_product(price="100.00", stock=0)
    old = str(TODAY + timedelta(days=1))
    await _send(api, branch, _line(product, 3, expires_on=old), receipt_no="RCV-OLD")
    await _send(api, branch, _line(product, 2, expires_on=str(TODAY + timedelta(days=60))),
                receipt_no="RCV-NEW")
    row, got = await _batches(db, branch, product)
    assert row.stock_quantity == 5
    # the day after the first batch's expiry date
    later = TODAY + timedelta(days=2)
    parts, short = inventory_batches.allocate(got, row.stock_quantity, 3, {}, today=later)
    assert short == 1 and [(b.lot_code, n) for b, n in parts] == [("RCV-NEW", 2)]
    # expired units never become "legacy" stock either
    parts, short = inventory_batches.allocate(got, row.stock_quantity, 5, {}, today=later)
    assert short == 3

    assert await batch_expiry.sweep(db, today=later) >= 1
    row, got = await _batches(db, branch, product)
    assert row.stock_quantity == 2
    first = next(b for b in got if b.lot_code == "RCV-OLD")
    assert (first.remaining, first.expired_units) == (0, 3) and first.expired_at is not None
    # a second sweep changes nothing
    await batch_expiry.sweep(db, today=later)
    row, _ = await _batches(db, branch, product)
    assert row.stock_quantity == 2


async def test_review_price_rules(api, db, world, make_product):
    branch = world.branch_id
    product = await make_product(price="200.00", stock=0)
    r = await _send(api, branch, _line(product, 5, expires_on=str(TODAY + timedelta(days=30)),
                                       mrp="200.00"), receipt_no="RCV-REV")
    batch_id = r[0]["batch_id"]
    url = f"/internal/supply/batches/{batch_id}/price"
    ok = await api.post(url, headers=AUTH, json={"branch_id": str(branch), "selling_price": "195.00"})
    assert ok.status_code == 200 and ok.json()["selling_price"] == "195.00", ok.text
    db.expire_all()
    assert (await db.get(InventoryBatch, uuid.UUID(batch_id))).selling_price == Decimal("195.00")
    above = await api.post(url, headers=AUTH, json={"branch_id": str(branch), "selling_price": "230.00"})
    assert above.status_code == 422
    other = await api.post(url, headers=AUTH,
                           json={"branch_id": str(uuid.uuid4()), "selling_price": "190.00"})
    assert other.status_code == 404
    assert (await api.post(url, json={"branch_id": str(branch), "selling_price": "190"})
            ).status_code == 401


async def test_expiring_list_follows_alert_days(api, db, world, make_product):
    from app.models.product import Product

    branch = world.branch_id
    soon, later = await make_product(price="50.00", stock=0), await make_product(price="50.00", stock=0)
    await _send(api, branch, _line(soon, 1, expires_on=str(TODAY + timedelta(days=20))),
                _line(later, 1, expires_on=str(TODAY + timedelta(days=45))), receipt_no="RCV-LIST")
    r = await api.get("/internal/supply/expiring", headers=AUTH, params={"branch_id": str(branch)})
    names = {i["product_id"]: i for i in r.json()["items"]}
    assert str(soon) in names and str(later) not in names  # 30 days by default
    assert names[str(soon)]["state"] == "near" and names[str(soon)]["alert_days"] == 30
    (await db.get(Product, later)).expiry_alert_days = 60
    await db.commit()
    r = await api.get("/internal/supply/expiring", headers=AUTH, params={"branch_id": str(branch)})
    assert str(later) in {i["product_id"] for i in r.json()["items"]}
