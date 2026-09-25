"""
Keeping our picture of SribeesExpress in step with SribeesExpress: the postal
cities it delivers to, the pickup outlets our branches are, and a sweep that
catches up shipments whose webhooks never arrived.

All three run against the fake courier's HTTP API, through the same client
code production uses.
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select


async def test_the_postal_city_directory_follows_the_courier(api, db, world, make_admin, fake_courier):
    from app.models.branch import PostalCityDirectory

    boss = await make_admin("super_admin", api=api)
    new_town = f"ZZ Newtown {uuid.uuid4().hex[:6]}"
    fake_courier.postal_cities = [
        {"id": world.courier_postal_city_id, "name": world.postal_city, "district": world.district,
         "province": f"{world.province} Province"},
        {"id": 700000 + uuid.uuid4().int % 99999, "name": new_town, "district": world.district,
         "province": f"{world.province} Province"},
    ]
    r = await api.post("/api/v1/admin/courier/postal-cities/sync", headers=boss.headers)
    assert r.status_code == 200, r.text

    db.expire_all()
    row = (await db.execute(select(PostalCityDirectory).where(
        PostalCityDirectory.postal_city == new_town))).scalar_one_or_none()
    assert row is not None, "a postal city the courier added appears in our directory"
    assert row.province == world.province, "'X Province' is stored as 'X', one spelling everywhere"
    assert row.is_launched is False, "new areas are not switched on for customers by a sync"

    # Syncing again changes nothing: idempotent.
    r = await api.post("/api/v1/admin/courier/postal-cities/sync", headers=boss.headers)
    assert r.status_code == 200, r.text
    n = len((await db.execute(select(PostalCityDirectory).where(
        PostalCityDirectory.postal_city == new_town))).scalars().all())
    assert n == 1

    manager = await make_admin("branch_manager", api=api)
    r = await api.post("/api/v1/admin/courier/postal-cities/sync", headers=manager.headers)
    assert r.status_code == 403


async def test_outlet_sync_and_the_reconciliation_sweep(api, world, make_admin, make_customer, make_product,
                                                        place_order, fake_courier):
    boss = await make_admin("super_admin", api=api)
    r = await api.post("/api/v1/admin/courier/outlets/sync", headers=boss.headers)
    assert r.status_code == 200, r.text

    customer = await make_customer()
    await place_order(customer, await make_product(), 1)
    r = await api.post("/api/v1/admin/courier/sync", headers=boss.headers)
    assert r.status_code == 200, r.text


@pytest.mark.parametrize("path", ["/api/v1/admin/courier/cod-balance", "/api/v1/admin/courier/remittances"])
async def test_courier_money_views_are_super_admin_only(api, world, make_admin, fake_courier, path):
    manager = await make_admin("marketing_manager", api=api)
    r = await api.get(path, headers=manager.headers)
    assert r.status_code == 403
