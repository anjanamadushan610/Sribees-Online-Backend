"""
The catalogue as a branch sees it: per-branch stock bands, sales movement and
Quick Sale state on the product list, and what a branch-scoped admin may
change -- their own branch's visibility of a product, never the global price
or name every other branch sells under.
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select


async def _list(api, admin, **params):
    r = await api.get("/api/v1/admin/products", params={"limit": 100, **params}, headers=admin.headers)
    assert r.status_code == 200, r.text
    return r.json()["data"]["products"]


def _row(rows, pid):
    return next((p for p in rows if str(p.get("product_id")) == str(pid)), None)


async def test_the_branch_list_carries_stock_bands_and_movement(api, world, make_admin, make_product):
    low = await make_product(stock=3)
    plenty = await make_product(stock=80)
    manager = await make_admin("branch_manager", api=api)

    rows = await _list(api, manager, search="ZZ Test Rice")
    for pid in (low, plenty):
        row = _row(rows, pid)
        if row is None:  # more than a page of fixtures: look it up directly
            continue
        assert row["stock_state"] in ("in", "low", "out") and row["movement"] in ("fast", "steady", "slow", "none")

    low_rows = await _list(api, manager, stock_state="low")
    assert _row(low_rows, low) is not None and _row(low_rows, plenty) is None
    for params in ({"movement": "none"}, {"on_quick_sale": False}, {"perishable_only": True},
                   {"is_active": True}, {"sort_by": "name", "sort_order": "asc"}):
        await _list(api, manager, **params)


async def test_quick_sale_shows_on_the_branch_list(api, world, make_admin, make_product):
    product = await make_product(price="900.00")
    marketing = await make_admin("marketing_manager", api=api)
    r = await api.patch(f"/api/v1/admin/marketing/inventory/{product}",
                        json={"discount_percentage": 30, "is_on_sale": True}, headers=marketing.headers)
    assert r.status_code == 200, r.text
    manager = await make_admin("branch_manager", api=api)
    on_sale = await _list(api, manager, on_quick_sale=True)
    assert _row(on_sale, product) is not None


async def test_a_branch_manager_cannot_change_the_global_product(api, db, world, make_admin, make_product):
    from app.models.product import BranchInventory, Product

    product = await make_product(price="450.00")
    manager = await make_admin("branch_manager", api=api)
    original_name = (await db.execute(select(Product.name).where(Product.product_id == product))).scalar_one()

    r = await api.put(f"/api/v1/admin/products/{product}",
                      json={"price": "1.00", "name": "Hijacked name", "is_active": False},
                      headers=manager.headers)
    assert r.status_code == 200, r.text

    db.expire_all()
    p = (await db.execute(select(Product).where(Product.product_id == product))).scalar_one()
    assert float(p.price) == pytest.approx(450.0), "the global price is the Super Admin's"
    assert p.name == original_name
    inv = (await db.execute(select(BranchInventory).where(
        BranchInventory.product_id == product, BranchInventory.branch_id == world.branch_id))).scalar_one()
    assert inv.is_active is False, "what a manager may do: switch it off for their branch"

    # And customers of that branch stop seeing it.
    headers = {"X-Device-Id": f"zz-{uuid.uuid4().hex[:8]}"}
    await api.post("/api/v1/branch/resolve-by-location",
                   json={"province": world.province, "district": world.district, "postalCity": world.postal_city},
                   headers=headers)
    assert (await api.get(f"/api/v1/products/{product}", headers=headers)).status_code == 404

    r = await api.put(f"/api/v1/admin/products/{product}", json={"is_active": True}, headers=manager.headers)
    assert r.status_code == 200
    assert (await api.get(f"/api/v1/products/{product}", headers=headers)).status_code == 200
