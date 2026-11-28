"""
A branch's fulfilment partner (Sribees Supply, owner 2026-11-27; migration 132): the
partner sets the price and stock of the branch's Other Products through Supply, and a
branch-scoped admin cannot change those prices while the partner is working.
"""
from __future__ import annotations

import hashlib
import secrets
from decimal import Decimal

import pytest

TOKEN = secrets.token_urlsafe(32)
AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture(autouse=True)
def _partner_on(monkeypatch):
    from app.config.settings import settings

    monkeypatch.setattr(settings, "supply_service_token_sha256", hashlib.sha256(TOKEN.encode()).hexdigest())
    monkeypatch.setattr(settings, "supply_allowed_ips", "")
    monkeypatch.setattr(settings, "supply_partner_enabled", True)


async def _group(db, world, pid, group):
    from sqlalchemy import select

    from app.models.product import BranchInventory

    inv = (await db.execute(select(BranchInventory).where(
        BranchInventory.product_id == pid, BranchInventory.branch_id == world.branch_id))).scalar_one()
    inv.sourcing_group = group
    await db.commit()
    return inv


async def _inv(db, world, pid):
    from sqlalchemy import select

    from app.models.product import BranchInventory

    db.expire_all()
    return (await db.execute(select(BranchInventory).where(
        BranchInventory.product_id == pid, BranchInventory.branch_id == world.branch_id))).scalar_one()


async def _flag(api, world, active=True):
    r = await api.put(f"/internal/supply/branches/{world.branch_id}/partner", json={"active": active},
                      headers=AUTH)
    assert r.status_code == 200, r.text


async def test_the_partner_routes_are_off_by_default_and_locked(api, monkeypatch, world):
    from app.config.settings import settings

    url = f"/internal/supply/branches/{world.branch_id}/partner"
    assert (await api.put(url, json={"active": True})).status_code == 401
    monkeypatch.setattr(settings, "supply_partner_enabled", False)
    assert (await api.put(url, json={"active": True}, headers=AUTH)).status_code == 404


async def test_partner_sets_price_and_stock_of_other_products_only(api, db, world, make_product):
    other = await make_product(price="200.00", stock=20)
    store = await make_product(price="300.00", stock=20)
    await _group(db, world, other, "external_daily")
    await _group(db, world, store, "store")

    # the partner is not working yet: nothing is its own
    r = await api.put("/internal/supply/partner-price", headers=AUTH, json={
        "branch_id": str(world.branch_id), "product_id": str(other), "price": "210.00"})
    assert r.status_code == 409
    await _flag(api, world)

    r = await api.put("/internal/supply/partner-price", headers=AUTH, json={
        "branch_id": str(world.branch_id), "product_id": str(other), "price": "210.00"})
    assert r.status_code == 200, r.text
    assert (await _inv(db, world, other)).branch_price == Decimal("210.00")
    r = await api.put("/internal/supply/partner-price", headers=AUTH, json={
        "branch_id": str(world.branch_id), "product_id": str(store), "price": "1.00"})
    assert r.status_code == 409  # Our Store products keep the manager's price

    r = await api.put("/internal/supply/partner-stock", headers=AUTH, json={
        "branch_id": str(world.branch_id),
        "items": [{"product_id": str(other), "stock": 7}, {"product_id": str(store), "stock": 0}]})
    assert r.status_code == 200, r.text
    assert [x["result"] for x in r.json()["results"]] == ["set", "not_partner"]
    assert (await _inv(db, world, other)).stock_quantity == 7
    assert (await _inv(db, world, store)).stock_quantity == 20


async def test_a_branch_admin_cannot_change_the_partners_price(api, db, world, make_product, make_admin):
    other = await make_product(price="200.00")
    store = await make_product(price="300.00")
    await _group(db, world, other, "external_slow")
    await _group(db, world, store, "store")
    await _flag(api, world)
    manager = await make_admin("branch_manager", api=api)
    boss = await make_admin("super_admin", api=api)
    other_id = (await _inv(db, world, other)).inventory_id
    store_id = (await _inv(db, world, store)).inventory_id

    r = await api.put(f"/api/v1/admin/inventory/{other_id}", headers=manager.headers,
                      json={"branch_price": "150.00"})
    assert r.status_code == 409, r.text
    r = await api.post("/api/v1/admin/inventory", headers=manager.headers, json={
        "product_id": str(other), "branch_id": str(world.branch_id), "branch_price": "150.00",
        "stock_quantity": 10, "is_active": True})
    assert r.status_code == 409, r.text  # the Price app's path
    r = await api.put(f"/api/v1/admin/inventory/{store_id}", headers=manager.headers,
                      json={"branch_price": "290.00"})
    assert r.status_code == 200, r.text  # Our Store: the manager's
    r = await api.put(f"/api/v1/admin/inventory/{other_id}", headers=boss.headers,
                      json={"branch_price": "150.00"})
    assert r.status_code == 200, r.text  # Super Admin may

    # the partner stops (clean exit): the manager prices it again
    await _flag(api, world, active=False)
    r = await api.put(f"/api/v1/admin/inventory/{other_id}", headers=manager.headers,
                      json={"branch_price": "160.00"})
    assert r.status_code == 200, r.text


async def test_no_lock_while_the_feature_is_off(api, db, world, make_product, make_admin, monkeypatch):
    from app.config.settings import settings

    other = await make_product(price="200.00")
    await _group(db, world, other, "external_daily")
    await _flag(api, world)
    monkeypatch.setattr(settings, "supply_partner_enabled", False)
    manager = await make_admin("branch_manager", api=api)
    inv = await _inv(db, world, other)
    r = await api.put(f"/api/v1/admin/inventory/{inv.inventory_id}", headers=manager.headers,
                      json={"branch_price": "150.00"})
    assert r.status_code == 200, r.text


async def test_a_loose_products_partner_count_goes_in_grams(api, db, world, make_product):
    from app.models.product import Product

    pid = await make_product(price="300.00", stock=0)
    product = await db.get(Product, pid)
    product.stock_mode = "loose"
    await db.commit()
    await _group(db, world, pid, "external_daily")
    await _flag(api, world)
    r = await api.put("/internal/supply/partner-stock", headers=AUTH, json={
        "branch_id": str(world.branch_id), "items": [{"product_id": str(pid), "stock": 0}]})
    assert r.json()["results"][0]["result"] == "needs_grams"
    r = await api.put("/internal/supply/partner-stock", headers=AUTH, json={
        "branch_id": str(world.branch_id),
        "items": [{"product_id": str(pid), "stock": 0, "grams": 5500}]})
    assert r.json()["results"][0]["result"] == "set", r.text
    assert (await _inv(db, world, pid)).stock_grams == 5500


async def test_grams_for_a_counted_product_are_refused_not_turned_into_a_count(
        api, db, world, make_product):
    pid = await make_product(price="50.00", stock=9)
    await _group(db, world, pid, "external_daily")
    await _flag(api, world)
    r = await api.put("/internal/supply/partner-stock", headers=AUTH, json={
        "branch_id": str(world.branch_id),
        "items": [{"product_id": str(pid), "stock": 0, "grams": 4000}]})
    assert r.json()["results"][0]["result"] == "mode_mismatch"
    assert (await _inv(db, world, pid)).stock_quantity == 9
