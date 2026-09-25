"""
Who runs the business: admin accounts, delegated staff and their roles, and
what admins see and change about customers (profiles, status, loyalty, promos,
cancellation history).

The access rules under test are the ones an insider attack would probe: a
Branch Manager cannot mint an admin, a staff account can never exceed the
permissions of the admin who created it, and deactivating an admin takes their
staff with them.
"""
from __future__ import annotations

import uuid

import pytest


def _email(tag="zz") -> str:
    return f"{tag}.{uuid.uuid4().hex[:10]}@sribees-ci.lk"


def _ip() -> dict:
    return {"X-Forwarded-For": f"203.0.{uuid.uuid4().int % 250}.{uuid.uuid4().int % 250}"}


async def _login(api, email, password):
    return await api.post("/api/v1/admin/auth/login", json={"email": email, "password": password}, headers=_ip())


# ---------------------------------------------------------------------------
# Admin accounts (Super Admin only)
# ---------------------------------------------------------------------------


async def test_admin_account_lifecycle(api, world, make_admin):
    boss = await make_admin("super_admin", api=api)
    email = _email("manager")
    r = await api.post(
        "/api/v1/admin/users",
        json={"email": email, "password": "Manager-Pass-1", "full_name": "ZZ Manager",
              "role": "branch_manager", "branch_id": str(world.branch_id)},
        headers=boss.headers,
    )
    assert r.status_code == 201, r.text
    new_id = r.json()["data"]["admin_id"]

    r = await api.post(
        "/api/v1/admin/users",
        json={"email": email.upper(), "password": "Manager-Pass-1", "full_name": "Dup", "role": "branch_manager",
              "branch_id": str(world.branch_id)},
        headers=boss.headers,
    )
    assert r.status_code == 400, "email is unique, whatever the case"

    r = await api.post(
        "/api/v1/admin/users",
        json={"email": _email(), "password": "Manager-Pass-1", "full_name": "No Branch", "role": "branch_manager"},
        headers=boss.headers,
    )
    assert r.status_code == 400, "a scoped role needs a branch"

    assert (await _login(api, email, "Manager-Pass-1")).status_code == 200

    r = await api.get("/api/v1/admin/users", headers=boss.headers)
    assert r.status_code == 200 and new_id in r.text

    r = await api.put(f"/api/v1/admin/users/{new_id}", json={"full_name": "ZZ Renamed"}, headers=boss.headers)
    assert r.status_code == 200, r.text

    r = await api.delete(f"/api/v1/admin/users/{boss.admin_id}", headers=boss.headers)
    assert r.status_code == 400, "a Super Admin cannot lock themselves out"
    r = await api.delete(f"/api/v1/admin/users/{new_id}", headers=boss.headers)
    assert r.status_code == 200, r.text
    assert (await _login(api, email, "Manager-Pass-1")).status_code in (401, 403)


@pytest.mark.parametrize("role", ["branch_manager", "marketing_manager", "inventory_manager", "customer_support"])
async def test_only_the_super_admin_manages_admin_accounts(api, world, make_admin, role):
    who = await make_admin(role, api=api)
    r = await api.get("/api/v1/admin/users", headers=who.headers)
    assert r.status_code == 403
    r = await api.post(
        "/api/v1/admin/users",
        json={"email": _email(), "password": "Whatever-Pass-1", "full_name": "Escalate", "role": "super_admin"},
        headers=who.headers,
    )
    assert r.status_code == 403, "no role may mint a Super Admin but a Super Admin"


# ---------------------------------------------------------------------------
# Staff and custom roles
# ---------------------------------------------------------------------------


async def _catalog(api, admin) -> tuple[list[dict], set[str]]:
    r = await api.get("/api/v1/admin/roles/catalog", headers=admin.headers)
    assert r.status_code == 200, r.text
    body = r.json()
    perms = body["permissions"]
    mine = set(body.get("my_permission_ids") or body.get("myPermissionIds") or [])
    return perms, mine


def _pid(p: dict) -> str:
    return str(p.get("permission_id") or p.get("permissionId"))


async def test_a_manager_delegates_a_subset_of_their_own_permissions(api, db, world, make_admin):
    manager = await make_admin("branch_manager", api=api)
    perms, mine = await _catalog(api, manager)
    assert perms and mine, "the catalogue and the manager's own grants are both non-empty"
    held = [p for p in perms if _pid(p) in mine]
    not_held = [p for p in perms if _pid(p) not in mine]
    orders_read = next(p for p in held if p["resource"] == "orders" and p["action"] == "read")

    # Beyond their own grants: refused.
    if not_held:
        r = await api.post(
            "/api/v1/admin/staff",
            json={"email": _email("staff"), "password": "Staff-Pass-12", "fullName": "Greedy Staff",
                  "newRole": {"name": f"Greedy {uuid.uuid4().hex[:4]}", "permissionIds": [_pid(not_held[0])]}},
            headers=manager.headers,
        )
        assert r.status_code == 400, r.text

    email = _email("staff")
    r = await api.post(
        "/api/v1/admin/staff",
        json={"email": email, "password": "Staff-Pass-12", "fullName": "Order Clerk",
              "newRole": {"name": f"Clerk {uuid.uuid4().hex[:4]}", "permissionIds": [_pid(orders_read)]}},
        headers=manager.headers,
    )
    assert r.status_code == 201, r.text
    staff_id = r.json()["staff"]["adminId"] if "staff" in r.json() else r.json()["data"]["adminId"]

    r = await api.get("/api/v1/admin/staff", headers=manager.headers)
    assert r.status_code == 200 and staff_id in r.text
    r = await api.get("/api/v1/admin/roles", headers=manager.headers)
    assert r.status_code == 200

    login = await _login(api, email, "Staff-Pass-12")
    assert login.status_code == 200, login.text
    staff_headers = {"Authorization": f"Bearer {login.json()['data']['token']}"}
    assert (await api.get("/api/v1/admin/orders", headers=staff_headers)).status_code == 200
    assert (await api.get("/api/v1/admin/products", headers=staff_headers)).status_code == 403
    # Hierarchy is one level deep: staff never create staff.
    r = await api.post(
        "/api/v1/admin/staff",
        json={"email": _email(), "password": "Staff-Pass-12", "fullName": "Sub", "roleId": str(uuid.uuid4())},
        headers=staff_headers,
    )
    assert r.status_code == 403

    r = await api.put(f"/api/v1/admin/staff/{staff_id}", json={"fullName": "Senior Clerk"}, headers=manager.headers)
    assert r.status_code == 200, r.text

    # A colleague in the same branch who lacks the staff's permissions cannot
    # take the account over: editing it means resetting its password.
    marketing = await make_admin("marketing_manager", api=api)
    r = await api.put(f"/api/v1/admin/staff/{staff_id}", json={"password": "Hijacked-Pass-1"},
                      headers=marketing.headers)
    assert r.status_code == 403, r.text
    assert (await _login(api, email, "Hijacked-Pass-1")).status_code == 401

    # Nor can a manager of another branch.
    import uuid as _uuid

    from app.models.branch import Branch

    other_branch = _uuid.uuid4()
    db.add(Branch(branch_id=other_branch, name="ZZ Elsewhere", code=f"ZZE{_uuid.uuid4().hex[:6]}",
                  province=world.province, district=world.district, is_active=True, delivery_runs=[]))
    await db.commit()
    elsewhere = await make_admin("branch_manager", branch_id=other_branch, api=api)
    r = await api.put(f"/api/v1/admin/staff/{staff_id}", json={"isActive": False}, headers=elsewhere.headers)
    assert r.status_code == 403, r.text

    r = await api.delete(f"/api/v1/admin/staff/{staff_id}", headers=manager.headers)
    assert r.status_code == 200, r.text
    assert (await _login(api, email, "Staff-Pass-12")).status_code in (401, 403)


async def test_deactivating_an_admin_takes_their_staff_with_them(api, world, make_admin):
    boss = await make_admin("super_admin", api=api)
    manager = await make_admin("branch_manager", api=api)
    perms, mine = await _catalog(api, manager)
    pid = _pid(next(p for p in perms if _pid(p) in mine))
    email = _email("staff")
    r = await api.post(
        "/api/v1/admin/staff",
        json={"email": email, "password": "Staff-Pass-12", "fullName": "Orphan",
              "newRole": {"name": f"R {uuid.uuid4().hex[:4]}", "permissionIds": [pid]}},
        headers=manager.headers,
    )
    assert r.status_code == 201, r.text
    r = await api.delete(f"/api/v1/admin/users/{manager.admin_id}", headers=boss.headers)
    assert r.status_code == 200, r.text
    assert (await _login(api, email, "Staff-Pass-12")).status_code in (401, 403)


# ---------------------------------------------------------------------------
# Customers, as admins see them
# ---------------------------------------------------------------------------


async def test_customer_directory(api, world, make_customer, make_product, place_order, make_admin):
    customer = await make_customer()
    await place_order(customer, await make_product(), 1)
    boss = await make_admin("super_admin", api=api)

    r = await api.get("/api/v1/admin/customers", params={"search": customer.phone[-9:]}, headers=boss.headers)
    assert r.status_code == 200, r.text
    assert str(customer.user_id) in r.text
    for params in [{"is_blocked": False}, {"deleted": True}, {"limit": 5, "page": 2}]:
        r = await api.get("/api/v1/admin/customers", params=params, headers=boss.headers)
        assert r.status_code == 200, (params, r.text)

    r = await api.get(f"/api/v1/admin/customers/{customer.user_id}/profile", headers=boss.headers)
    assert r.status_code == 200, r.text
    r = await api.get(f"/api/v1/admin/customers/{customer.user_id}/orders", headers=boss.headers)
    assert r.status_code == 200, r.text
    assert r.text.count("order_number") >= 1


async def test_blocking_a_customer_stops_their_session(api, world, make_customer, make_admin):
    customer = await make_customer()
    support = await make_admin("customer_support", branch_id=None, api=api)
    r = await api.put(f"/api/v1/admin/customers/{customer.user_id}/status", params={"is_active": False},
                      headers=support.headers)
    assert r.status_code == 200, r.text
    r = await api.get("/api/v1/auth/me", headers=customer.headers)
    assert r.status_code == 403, "an inactive account's live token stops working"
    r = await api.put(f"/api/v1/admin/customers/{customer.user_id}/status", params={"is_active": True},
                      headers=support.headers)
    assert r.status_code == 200
    assert (await api.get("/api/v1/auth/me", headers=customer.headers)).status_code == 200

    marketing = await make_admin("marketing_manager", api=api)
    r = await api.put(f"/api/v1/admin/customers/{customer.user_id}/status", params={"is_active": False},
                      headers=marketing.headers)
    assert r.status_code == 403


async def test_super_admin_dashboards(api, world, make_admin):
    boss = await make_admin("super_admin", api=api)
    for path in ["/api/v1/admin/dashboard", "/api/v1/admin/analytics/revenue", "/api/v1/admin/analytics/orders",
                 "/api/v1/admin/branch-options"]:
        r = await api.get(path, headers=boss.headers)
        assert r.status_code == 200, (path, r.text)
    manager = await make_admin("branch_manager", api=api)
    assert (await api.get("/api/v1/admin/dashboard", headers=manager.headers)).status_code == 403


# ---------------------------------------------------------------------------
# Loyalty tiers, customer promos, cancellation history
# ---------------------------------------------------------------------------


async def test_loyalty_tiers(api, world, make_customer, make_admin):
    boss = await make_admin("super_admin", api=api)
    points = 900000 + uuid.uuid4().int % 90000
    r = await api.post(
        "/api/v1/admin/loyalty/tiers",
        json={"name": f"ZZ Gold {points}", "name_si": "රන්", "min_points": points, "color": "#FFD700",
              "reward_discount_type": "percentage", "reward_discount_value": "5", "reward_valid_days": 30},
        headers=boss.headers,
    )
    assert r.status_code == 201, r.text
    tier_id = r.json()["data"]["tier_id"]

    r = await api.post("/api/v1/admin/loyalty/tiers", json={"name": "Dup", "min_points": points},
                       headers=boss.headers)
    assert r.status_code == 400, "two tiers cannot start at the same points"

    r = await api.put(f"/api/v1/admin/loyalty/tiers/{tier_id}", json={"name": "ZZ Platinum"}, headers=boss.headers)
    assert r.status_code == 200, r.text
    r = await api.get("/api/v1/admin/loyalty/tiers", headers=boss.headers)
    assert "ZZ Platinum" in r.text

    customer = await make_customer()
    r = await api.get(f"/api/v1/admin/loyalty/customers/{customer.user_id}", headers=boss.headers)
    assert r.status_code == 200, r.text

    marketing = await make_admin("marketing_manager", api=api)
    assert (await api.get("/api/v1/admin/loyalty/tiers", headers=marketing.headers)).status_code == 200
    r = await api.delete(f"/api/v1/admin/loyalty/tiers/{tier_id}", headers=marketing.headers)
    assert r.status_code == 403, "marketing reads tiers; only the Super Admin changes them"

    r = await api.delete(f"/api/v1/admin/loyalty/tiers/{tier_id}", headers=boss.headers)
    assert r.status_code == 200, r.text


async def test_a_personal_promo_for_one_customer(api, world, make_customer, make_product, place_order, make_admin):
    customer = await make_customer()
    marketing = await make_admin("marketing_manager", api=api)
    r = await api.get("/api/v1/admin/customers/promo-presets", headers=marketing.headers)
    assert r.status_code == 200, r.text

    # A branch's marketing may only reach customers that branch has served.
    r = await api.post(f"/api/v1/admin/customers/{customer.user_id}/promo", json={"preset": "apology"},
                       headers=marketing.headers)
    assert r.status_code == 404, r.text
    await place_order(customer, await make_product(), 1)
    r = await api.post(f"/api/v1/admin/customers/{customer.user_id}/promo", json={"preset": "apology"},
                       headers=marketing.headers)
    assert r.status_code == 201, r.text
    code = r.json()["data"]["code"]

    # It is theirs: it shows in their coupons, and nobody else's.
    r = await api.get("/api/v1/coupons/available", headers=customer.headers)
    assert r.status_code == 200
    other = await make_customer()
    r = await api.get("/api/v1/coupons/available", headers=other.headers)
    assert code not in r.text

    support = await make_admin("customer_support", branch_id=None, api=api)
    r = await api.post(f"/api/v1/admin/customers/{customer.user_id}/promo", json={"preset": "winback"},
                       headers=support.headers)
    assert r.status_code == 403


async def test_cancellation_history(api, world, make_customer, make_product, place_order, make_admin):
    customer = await make_customer()
    order = await place_order(customer, await make_product(), 1)
    r = await api.post(f"/api/v1/orders/{order['order_id']}/cancel", headers=customer.headers)
    assert r.status_code == 200, r.text

    boss = await make_admin("super_admin", api=api)
    r = await api.get("/api/v1/admin/cancellations", params={"user_id": str(customer.user_id)}, headers=boss.headers)
    assert r.status_code == 200, r.text
    assert order["order_number"] in r.text
    for path in ["/api/v1/admin/cancellations/customers",
                 f"/api/v1/admin/cancellations/customers/{customer.user_id}/links"]:
        r = await api.get(path, headers=boss.headers)
        assert r.status_code == 200, (path, r.text)
