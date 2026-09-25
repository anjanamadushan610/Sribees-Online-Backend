"""
Running the branches: the branch records themselves, their stock, stock moving
between branches, which categories each branch shows, and where the business
delivers (service areas and the postal-city directory).

Two rules carry most of the weight here: a scoped admin only ever touches their
own branch, and stock is conserved -- a transfer takes units out of one branch
exactly once and puts the same number into the other exactly once.
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select


def _code() -> str:
    return f"ZZ{uuid.uuid4().hex[:6].upper()}"


async def _new_branch(api, boss, world, **over) -> dict:
    body = {"name": f"ZZ Branch {uuid.uuid4().hex[:4]}", "code": _code(), "province": world.province,
            "district": world.district, "phone": "0112345678", **over}
    r = await api.post("/api/v1/admin/branches", json=body, headers=boss.headers)
    assert r.status_code == 201, r.text
    return r.json()["data"]


def _bid(b: dict) -> str:
    return str(b.get("branch_id") or b.get("branchId"))


async def _inventory_stock(db, product_id, branch_id) -> int | None:
    from app.models.product import BranchInventory

    db.expire_all()
    return (
        await db.execute(
            select(BranchInventory.stock_quantity).where(
                BranchInventory.product_id == product_id, BranchInventory.branch_id == branch_id
            )
        )
    ).scalar_one_or_none()


# ---------------------------------------------------------------------------
# Branches
# ---------------------------------------------------------------------------


async def test_branch_lifecycle(api, world, make_admin):
    boss = await make_admin("super_admin", api=api)
    b = await _new_branch(api, boss, world)
    bid = _bid(b)
    assert (b.get("is_active") if "is_active" in b else b.get("isActive")) is False, (
        "a new branch starts dark until someone who runs it switches it on"
    )

    r = await api.post("/api/v1/admin/branches", json={"name": "Dup", "code": b["code"], "province": "X"},
                       headers=boss.headers)
    assert r.status_code == 400, "branch codes are unique"

    r = await api.put(f"/api/v1/admin/branches/{bid}", json={"name": "ZZ Renamed Branch"}, headers=boss.headers)
    assert r.status_code == 200, r.text

    r = await api.put(
        f"/api/v1/admin/branches/{bid}/delivery-runs",
        json={"delivery_runs": [{"label": "Morning", "dispatch_time": "10:00", "cutoff_minutes": 30}]},
        headers=boss.headers,
    )
    assert r.status_code == 200, r.text
    r = await api.put(
        f"/api/v1/admin/branches/{bid}/delivery-runs",
        json={"delivery_runs": [{"label": "Bad", "dispatch_time": "25:99"}]},
        headers=boss.headers,
    )
    assert r.status_code == 422

    r = await api.patch(f"/api/v1/admin/branches/{bid}/status", json={"is_active": True}, headers=boss.headers)
    assert r.status_code == 200, r.text

    r = await api.get("/api/v1/admin/branches", headers=boss.headers)
    assert bid in r.text

    # A branch with nothing hanging off it can be deleted -- including its
    # per-branch category rows, which used to make every delete a 500.
    r = await api.delete(f"/api/v1/admin/branches/{bid}", headers=boss.headers)
    assert r.status_code == 200, r.text


async def test_a_branch_in_use_cannot_be_deleted(api, world, make_admin, make_product):
    await make_product()
    boss = await make_admin("super_admin", api=api)
    r = await api.delete(f"/api/v1/admin/branches/{world.branch_id}", headers=boss.headers)
    assert r.status_code == 400, r.text
    assert "Cannot delete" in r.text


async def test_managers_run_only_their_own_branch(api, world, make_admin):
    boss = await make_admin("super_admin", api=api)
    other = _bid(await _new_branch(api, boss, world))
    manager = await make_admin("branch_manager", api=api)

    r = await api.get("/api/v1/admin/branches", headers=manager.headers)
    assert r.status_code == 200
    r = await api.patch(f"/api/v1/admin/branches/{world.branch_id}/status", json={"is_active": True},
                        headers=manager.headers)
    assert r.status_code == 200, r.text
    r = await api.patch(f"/api/v1/admin/branches/{other}/status", json={"is_active": True}, headers=manager.headers)
    assert r.status_code == 403
    for method, path, body in [
        ("POST", "/api/v1/admin/branches", {"name": "x", "code": _code(), "province": "x"}),
        ("PUT", f"/api/v1/admin/branches/{world.branch_id}", {"name": "mine now"}),
        ("DELETE", f"/api/v1/admin/branches/{other}", None),
    ]:
        r = await api.request(method, path, json=body, headers=manager.headers)
        assert r.status_code == 403, f"{method} {path} -> {r.status_code}"


# ---------------------------------------------------------------------------
# Inventory
# ---------------------------------------------------------------------------


async def test_inventory_in_a_branch(api, db, world, make_admin, make_product):
    manager = await make_admin("branch_manager", api=api)
    stocked = await make_product(stock=5)

    r = await api.get("/api/v1/admin/inventory", params={"low_stock_only": True}, headers=manager.headers)
    assert r.status_code == 200 and str(stocked) in r.text, "5 units is below the default threshold of 10"

    # A catalogue product not yet sold here shows under "catalog" until stocked.
    boss = await make_admin("super_admin", api=api)
    other_branch = _bid(await _new_branch(api, boss, world))
    r = await api.get("/api/v1/admin/inventory/catalog", params={"branch_id": other_branch, "limit": 100},
                      headers=boss.headers)
    assert r.status_code == 200, r.text
    total = r.json()["data"]["pagination"]["total"]
    seen = r.text
    page = 2
    while str(stocked) not in seen and (page - 1) * 100 < total:
        seen = (await api.get("/api/v1/admin/inventory/catalog",
                              params={"branch_id": other_branch, "limit": 100, "page": page},
                              headers=boss.headers)).text
        page += 1
    assert str(stocked) in seen

    r = await api.post(
        "/api/v1/admin/inventory",
        json={"product_id": str(stocked), "branch_id": other_branch, "branch_price": "275.00", "stock_quantity": 40},
        headers=boss.headers,
    )
    assert r.status_code == 201, r.text
    inv_id = r.json()["data"]["inventory_id"]
    assert await _inventory_stock(db, stocked, uuid.UUID(other_branch)) == 40

    r = await api.put(f"/api/v1/admin/inventory/{inv_id}", json={"stock_quantity": 35, "is_on_sale": True},
                      headers=boss.headers)
    assert r.status_code == 200, r.text
    assert await _inventory_stock(db, stocked, uuid.UUID(other_branch)) == 35

    # That row belongs to another branch: this manager cannot change it.
    r = await api.put(f"/api/v1/admin/inventory/{inv_id}", json={"stock_quantity": 0}, headers=manager.headers)
    assert r.status_code in (403, 404), r.text
    assert await _inventory_stock(db, stocked, uuid.UUID(other_branch)) == 35

    r = await api.put(f"/api/v1/admin/inventory/{inv_id}", json={"stock_quantity": -1}, headers=boss.headers)
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# Stock transfers
# ---------------------------------------------------------------------------


async def test_a_stock_transfer_moves_units_exactly_once(api, db, world, make_admin, make_product):
    boss = await make_admin("super_admin", api=api)
    product = await make_product(stock=30)
    dest = _bid(await _new_branch(api, boss, world))

    r = await api.post(
        "/api/v1/admin/transfers",
        json={"from_branch_id": str(world.branch_id), "to_branch_id": dest, "product_id": str(product),
              "quantity": 12, "notes": "Weekend rush"},
        headers=boss.headers,
    )
    assert r.status_code == 201, r.text
    tid = r.json()["data"]["transfer_id"]
    assert await _inventory_stock(db, product, world.branch_id) == 30, "nothing moves until approved"

    r = await api.post(f"/api/v1/admin/transfers/{tid}/complete", headers=boss.headers)
    assert r.status_code == 400, "a transfer cannot complete before it ships"

    for step in ["approve", "ship", "complete"]:
        r = await api.post(f"/api/v1/admin/transfers/{tid}/{step}", headers=boss.headers)
        assert r.status_code == 200, (step, r.text)

    assert await _inventory_stock(db, product, world.branch_id) == 18
    assert await _inventory_stock(db, product, uuid.UUID(dest)) == 12

    # Completing again must not add the units a second time.
    r = await api.post(f"/api/v1/admin/transfers/{tid}/complete", headers=boss.headers)
    assert r.status_code == 400
    assert await _inventory_stock(db, product, uuid.UUID(dest)) == 12

    r = await api.get("/api/v1/admin/transfers", params={"status": "completed"}, headers=boss.headers)
    assert r.status_code == 200 and tid in r.text


async def test_transfer_rules(api, db, world, make_admin, make_product):
    boss = await make_admin("super_admin", api=api)
    product = await make_product(stock=5)
    dest = _bid(await _new_branch(api, boss, world))
    base = {"from_branch_id": str(world.branch_id), "product_id": str(product)}

    r = await api.post("/api/v1/admin/transfers", json={**base, "to_branch_id": dest, "quantity": 50},
                       headers=boss.headers)
    assert r.status_code == 400 and "only has 5" in r.text
    r = await api.post("/api/v1/admin/transfers", json={**base, "to_branch_id": str(world.branch_id), "quantity": 1},
                       headers=boss.headers)
    assert r.status_code in (400, 422), "a branch cannot transfer to itself"

    r = await api.post("/api/v1/admin/transfers", json={**base, "to_branch_id": dest, "quantity": 2},
                       headers=boss.headers)
    tid = r.json()["data"]["transfer_id"]
    r = await api.post(f"/api/v1/admin/transfers/{tid}/reject", json={"reason": "Not needed"}, headers=boss.headers)
    assert r.status_code == 200, r.text
    r = await api.post(f"/api/v1/admin/transfers/{tid}/approve", headers=boss.headers)
    assert r.status_code == 400, "a rejected transfer stays rejected"
    assert await _inventory_stock(db, product, world.branch_id) == 5


# ---------------------------------------------------------------------------
# Branch categories
# ---------------------------------------------------------------------------


async def test_a_branch_hides_a_category(api, world, make_admin):
    manager = await make_admin("branch_manager", api=api)
    r = await api.get("/api/v1/admin/branch-categories", headers=manager.headers)
    assert r.status_code == 200, r.text
    rows = r.json()["data"]
    rows = rows if isinstance(rows, list) else rows.get("categories", [])
    if not rows:
        pytest.skip("no categories exist in this database yet")
    cat_id = rows[0].get("category_id") or rows[0].get("categoryId")
    for active in (False, True):
        r = await api.patch(f"/api/v1/admin/branch-categories/{cat_id}", json={"is_active": active},
                            headers=manager.headers)
        assert r.status_code == 200, r.text


# ---------------------------------------------------------------------------
# Service areas and the postal-city directory
# ---------------------------------------------------------------------------


async def _resolve(api, world):
    return await api.post(
        "/api/v1/branch/resolve-by-location",
        json={"province": world.province, "district": world.district, "postalCity": world.postal_city},
        headers={"X-Device-Id": f"zz-{uuid.uuid4().hex[:8]}"},
    )


async def test_locking_a_district_stops_service_there_at_once(api, world, make_admin):
    boss = await make_admin("super_admin", api=api)
    assert (await _resolve(api, world)).status_code == 200

    r = await api.patch(f"/api/v1/admin/service-areas/districts/{world.district}", json={"is_unlocked": False},
                        headers=boss.headers)
    assert r.status_code == 200, r.text
    assert (await _resolve(api, world)).status_code == 404

    r = await api.patch(f"/api/v1/admin/service-areas/districts/{world.district}", json={"is_unlocked": True},
                        headers=boss.headers)
    assert r.status_code == 200
    assert (await _resolve(api, world)).status_code == 200

    r = await api.get("/api/v1/admin/service-areas", headers=boss.headers)
    assert r.status_code == 200 and world.district in r.text
    r = await api.get(f"/api/v1/admin/service-areas/districts/{world.district}/postal-areas", headers=boss.headers)
    assert r.status_code == 200 and world.postal_city in r.text

    r = await api.post("/api/v1/admin/service-areas/postal-areas/deactivate",
                       json={"postal_cities": [world.postal_city]}, headers=boss.headers)
    assert r.status_code == 200, r.text
    assert (await _resolve(api, world)).status_code == 404
    r = await api.post(f"/api/v1/admin/service-areas/districts/{world.district}/postal-areas/activate",
                       json={"postal_cities": [world.postal_city]}, headers=boss.headers)
    assert r.status_code == 200, r.text
    assert (await _resolve(api, world)).status_code == 200

    manager = await make_admin("branch_manager", api=api)
    r = await api.patch(f"/api/v1/admin/service-areas/provinces/{world.province}", json={"is_unlocked": False},
                        headers=manager.headers)
    assert r.status_code == 403, "only the Super Admin decides where the business delivers"


async def test_postal_city_directory(api, world, make_admin):
    boss = await make_admin("super_admin", api=api)
    name = f"ZZ Village {uuid.uuid4().hex[:6]}"
    r = await api.post("/api/v1/admin/locations",
                       json={"postal_city": name, "district": world.district, "province": world.province},
                       headers=boss.headers)
    assert r.status_code == 201, r.text
    loc_id = r.json()["data"].get("id") or r.json()["data"].get("location_id")

    r = await api.post("/api/v1/admin/locations",
                       json={"postal_city": name, "district": world.district, "province": world.province},
                       headers=boss.headers)
    assert r.status_code in (400, 409), "postal city names are unique"

    r = await api.get("/api/v1/admin/locations", params={"district": world.district}, headers=boss.headers)
    # Names are stored title-cased ("Zz Village ..."), so compare without case.
    assert r.status_code == 200 and name.lower() in r.text.lower()
    r = await api.put(f"/api/v1/admin/locations/{loc_id}", json={"is_active": False}, headers=boss.headers)
    assert r.status_code == 200, r.text
    r = await api.delete(f"/api/v1/admin/locations/{loc_id}", headers=boss.headers)
    assert r.status_code == 200, r.text
