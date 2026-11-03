"""
Branch cashback, end to end on a real database (migration 099).

Only a Branch Manager or Super Admin may change it, never above the cap; other
roles keep saving stock and Quick Sale. And a price change through
POST /admin/inventory (the Price app's call) must not wipe the branch's
other settings.
"""
from __future__ import annotations

from sqlalchemy import select

INV = "/api/v1/admin/inventory"
MKT = "/api/v1/admin/marketing"


async def _row(db, product_id, branch_id):
    from app.models.product import BranchInventory

    db.expire_all()
    return (
        await db.execute(
            select(BranchInventory).where(
                BranchInventory.product_id == product_id, BranchInventory.branch_id == branch_id
            )
        )
    ).scalar_one()


async def test_only_branch_manager_and_super_admin_change_cashback(
    api, db, world, make_product, make_admin
):
    boss = await make_admin("super_admin", api=api)
    manager = await make_admin("branch_manager", api=api)
    stock_keeper = await make_admin("inventory_manager", api=api)
    marketer = await make_admin("marketing_manager", api=api)
    pid = await make_product(price="300.00")
    inv = (await _row(db, pid, world.branch_id)).inventory_id

    r = await api.put(f"{INV}/{inv}", json={"cashback_percentage": 2}, headers=stock_keeper.headers)
    assert r.status_code == 403, r.text
    r = await api.patch(f"{MKT}/inventory/{pid}", json={"cashback_percentage": 2}, headers=marketer.headers)
    assert r.status_code == 403, r.text

    r = await api.put(f"{INV}/{inv}", json={"cashback_percentage": 31}, headers=manager.headers)
    assert r.status_code == 400 and "30" in r.text, r.text
    r = await api.put(f"{INV}/{inv}", json={"cashback_percentage": 31}, headers=boss.headers)
    assert r.status_code == 400, r.text
    r = await api.put(f"{INV}/{inv}", json={"cashback_percentage": 2}, headers=manager.headers)
    assert r.status_code == 200 and r.json()["data"]["cashback_percentage"] == 2, r.text

    # Others still save stock with the whole form, cashback unchanged.
    r = await api.put(
        f"{INV}/{inv}", json={"stock_quantity": 40, "cashback_percentage": 2}, headers=stock_keeper.headers
    )
    assert r.status_code == 200, r.text
    r = await api.patch(
        f"{MKT}/inventory/{pid}", json={"discount_percentage": 5, "cashback_percentage": 2},
        headers=marketer.headers,
    )
    assert r.status_code == 200, r.text

    r = await api.get(INV, headers=stock_keeper.headers)
    assert r.json()["data"]["scope"]["can_edit_cashback"] is False
    assert r.json()["data"]["scope"]["cashback_branch_max_percent"] == 30.0
    r = await api.get(INV, headers=manager.headers)
    assert r.json()["data"]["scope"]["can_edit_cashback"] is True


async def test_a_price_change_keeps_the_branchs_other_settings(
    api, db, world, make_product, make_admin
):
    manager = await make_admin("branch_manager", api=api)
    pid = await make_product(price="300.00")
    inv = (await _row(db, pid, world.branch_id)).inventory_id
    r = await api.put(
        f"{INV}/{inv}",
        json={"cashback_percentage": 2, "discount_percentage": 10, "is_on_sale": True,
              "low_stock_threshold": 3, "stock_quantity": 7},
        headers=manager.headers,
    )
    assert r.status_code == 200, r.text

    # Exactly what the Price app sends for a branch price.
    r = await api.post(
        INV,
        json={"product_id": str(pid), "branch_id": str(world.branch_id), "branch_price": 320,
              "stock_quantity": 7, "is_active": True},
        headers=manager.headers,
    )
    assert r.status_code == 201, r.text

    row = await _row(db, pid, world.branch_id)
    assert float(row.branch_price) == 320
    assert (row.cashback_percentage, row.discount_percentage, row.is_on_sale, row.low_stock_threshold) == (
        2, 10, True, 3,
    )
