"""
Per-product referral commission, end to end on a real database (migration 081).

Who may set a rate and how high; that checkout freezes each line's rate and
coupon share; that a partner sees where the money came from (and never the
basket value); and that a return takes back exactly what the returned units
earned. Every amount is worked out in the comment beside it.
"""
from __future__ import annotations

import json
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.orm import selectinload

RC = "/api/v1/admin/referral-commission"


def _data(r):
    return r.json()["data"]


async def _inventory_id(db, product_id, branch_id):
    from app.models.product import BranchInventory

    return (
        await db.execute(
            select(BranchInventory.inventory_id).where(
                BranchInventory.product_id == product_id, BranchInventory.branch_id == branch_id
            )
        )
    ).scalar_one()


async def _partner(api, boss, *, lead_headers=None):
    """A lead created by the Super Admin, or an agent recruited by a lead."""
    email = f"zz.p.{uuid.uuid4().hex[:8]}@sribees-ci.lk"
    password = f"Zz{secrets.token_urlsafe(10)}9!"
    body = {"email": email, "password": password, "full_name": f"ZZ {email[:10]}"}
    if lead_headers is None:
        r = await api.post("/api/v1/admin/partners", json=body, headers=boss.headers)
    else:
        r = await api.post("/api/v1/partner/recruits", json=body, headers=lead_headers)
    assert r.status_code == 201, r.text
    r = await api.post(
        "/api/v1/partner/auth/login",
        json={"email": email, "password": password},
        headers={"X-Forwarded-For": f"192.0.2.{uuid.uuid4().int % 250}"},
    )
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['data']['token']}"}


async def _coupon(db, percent="10"):
    from app.models.coupon import Coupon

    now = datetime.now(timezone.utc)
    code = f"ZZ{uuid.uuid4().hex[:6].upper()}"
    db.add(
        Coupon(
            coupon_id=uuid.uuid4(),
            code=code,
            description="ZZ test offer",
            discount_type="percentage",
            discount_value=Decimal(percent),
            min_order_value=Decimal("0"),
            valid_from=now - timedelta(days=1),
            valid_until=now + timedelta(days=10),
            is_active=True,
            is_public=True,
        )
    )
    await db.commit()
    return code


async def _order(api, app_redis, world, customer, lines, **extra):
    """Several products in one cart, checked out through the real API."""
    from app.config.redis import RedisKeys
    from app.services.branch_service import _build_branch_context

    await app_redis.setex(
        RedisKeys.branch_context(str(customer.user_id)),
        600,
        json.dumps(_build_branch_context(world.branch_id, "ZZ", world.postal_city)),
    )
    for product_id, qty in lines:
        r = await api.post(
            "/api/v1/cart/items",
            json={"product_id": str(product_id), "quantity": qty, "price": "1.00", "name": "ZZ item"},
            headers=customer.headers,
        )
        assert r.status_code == 200, r.text
    r = await api.post(
        "/api/v1/orders",
        json={
            "delivery_address_id": str(customer.address_id),
            "contact_phone": customer.phone,
            "alternate_phone": "+94719999999",
            "payment_method": "COD",
            **extra,
        },
        headers=customer.headers,
    )
    assert r.status_code == 201, r.text
    return r.json()["data"]


# ---------------------------------------------------------------------------
# Who may set what
# ---------------------------------------------------------------------------


async def test_rates_are_set_by_role_capped_for_branches_and_audited(
    api, db, world, make_product, make_admin
):
    boss = await make_admin("super_admin", api=api)
    manager = await make_admin("branch_manager", api=api)
    stock_keeper = await make_admin("inventory_manager", api=api)
    pid = await make_product(price="1000.00")
    inv = await _inventory_id(db, pid, world.branch_id)

    # Product default: Super Admin only.
    r = await api.put(f"{RC}/products/{pid}", json={"percentage": 4}, headers=manager.headers)
    assert r.status_code == 403, r.text
    r = await api.put(f"{RC}/products/{pid}", json={"percentage": 4}, headers=boss.headers)
    assert r.status_code == 200, r.text
    assert _data(r)["effective_referral_commission"] == 4.0

    # Branch override: the manager, up to the cap (10% by default).
    r = await api.put(f"{RC}/inventory/{inv}", json={"percentage": 12}, headers=manager.headers)
    assert r.status_code == 400 and r.json()["detail"]["error"]["code"] == "ABOVE_CAP", r.text
    r = await api.put(f"{RC}/inventory/{inv}", json={"percentage": 6}, headers=manager.headers)
    assert r.status_code == 200, r.text
    assert _data(r)["effective_referral_commission"] == 6.0
    assert _data(r)["global_referral_commission_percentage"] == 4.0

    # Stock is not money: an Inventory Manager cannot touch it.
    r = await api.put(f"{RC}/inventory/{inv}", json={"percentage": 1}, headers=stock_keeper.headers)
    assert r.status_code == 403, r.text

    # The table shows all three numbers.
    r = await api.get("/api/v1/admin/inventory", headers=manager.headers)
    row = next(i for i in _data(r)["items"] if i["product_id"] == str(pid))
    assert (row["referral_commission_percentage"], row["global_referral_commission_percentage"],
            row["effective_referral_commission"]) == (6.0, 4.0, 6.0)

    # The cap is the Super Admin's to move; lowering it binds the next change.
    r = await api.put(f"{RC}/settings", json={"branch_max_percent": 5}, headers=manager.headers)
    assert r.status_code == 403
    r = await api.put(f"{RC}/settings", json={"branch_max_percent": 5}, headers=boss.headers)
    assert r.status_code == 200 and _data(r)["branch_max_percent"] == 5.0
    r = await api.put(f"{RC}/inventory/{inv}", json={"percentage": 5.5}, headers=manager.headers)
    assert r.status_code == 400, r.text
    # A Super Admin is not capped.
    r = await api.put(f"{RC}/inventory/{inv}", json={"percentage": 25}, headers=boss.headers)
    assert r.status_code == 200, r.text
    # Clearing the override falls back to the product default.
    r = await api.put(f"{RC}/inventory/{inv}", json={"percentage": None}, headers=manager.headers)
    assert r.status_code == 200 and _data(r)["effective_referral_commission"] == 4.0
    await api.put(f"{RC}/settings", json={"branch_max_percent": 10}, headers=boss.headers)

    # Every change is on record, newest first.
    r = await api.get(f"{RC}/products/{pid}/history", headers=boss.headers)
    changes = _data(r)["changes"]
    assert [c["new_percentage"] for c in changes] == [None, 25.0, 6.0, 4.0]
    assert changes[-1]["branch_id"] is None and changes[-1]["old_percentage"] is None


async def test_a_branch_manager_cannot_set_another_branchs_rate(
    api, db, world, make_product, make_admin
):
    from app.models.branch import Branch

    other = uuid.uuid4()
    db.add(Branch(branch_id=other, name="ZZ Other", code=f"ZZO{other.hex[:4]}",
                  province=world.province, district=world.district, is_active=True, delivery_runs=[]))
    await db.commit()
    outsider = await make_admin("branch_manager", branch_id=other, api=api)
    inv = await _inventory_id(db, await make_product(), world.branch_id)

    r = await api.put(f"{RC}/inventory/{inv}", json={"percentage": 3}, headers=outsider.headers)
    assert r.status_code == 403 and r.json()["detail"]["error"]["code"] == "OTHER_BRANCH", r.text


# ---------------------------------------------------------------------------
# The money
# ---------------------------------------------------------------------------


async def test_each_product_earns_its_own_rate_and_a_return_takes_back_only_its_units(
    api, db, app_redis, world, make_customer, make_product, make_admin, fake_courier
):
    from app.models.order import Order, OrderItem
    from app.models.referral import ReferralCommission
    from app.services.order_service import OrderService

    boss = await make_admin("super_admin", api=api)
    manager = await make_admin("branch_manager", api=api)
    lead = await _partner(api, boss)
    agent = await _partner(api, boss, lead_headers=lead)
    agent_code = _data(await api.get("/api/v1/partner/me", headers=agent))["referralCode"]

    rice = await make_product(price="1000.00")   # product default 5%
    salt = await make_product(price="500.00")    # never set: earns nothing
    tea = await make_product(price="200.00")     # branch override 10%
    assert (await api.put(f"{RC}/products/{rice}", json={"percentage": 5}, headers=boss.headers)).status_code == 200
    tea_inv = await _inventory_id(db, tea, world.branch_id)
    assert (await api.put(f"{RC}/inventory/{tea_inv}", json={"percentage": 10},
                          headers=manager.headers)).status_code == 200

    buyer = await make_customer()
    r = await api.post("/api/v1/referrals/redeem", json={"referral_code": agent_code}, headers=buyer.headers)
    assert r.status_code == 200, r.text

    # 2 x 1000 + 500 + 200 = 2700; 10% off = 270, shared 200 / 50 / 20.
    code = await _coupon(db)
    order = await _order(api, app_redis, world, buyer, [(rice, 2), (salt, 1), (tea, 1)], coupon_code=code)

    db.expire_all()
    items = {
        i.product_id: i
        for i in (await db.execute(select(OrderItem).where(OrderItem.order_id == order["order_id"]))).scalars()
    }
    assert {p: (i.referral_commission_percentage, i.coupon_discount) for p, i in items.items()} == {
        rice: (Decimal("5.00"), Decimal("200.00")),
        salt: (Decimal("0.00"), Decimal("50.00")),
        tea: (Decimal("10.00"), Decimal("20.00")),
    }
    assert sum(i.coupon_discount for i in items.values()) == Decimal(str(order["discount_amount"]))

    async def ledger():
        db.expire_all()
        rows = (await db.execute(
            select(ReferralCommission)
            .where(ReferralCommission.order_id == order["order_id"])
            .options(selectinload(ReferralCommission.lines))
        )).scalars().all()
        return {r.level: r for r in rows}

    # Level 1: rice 1800 x 5% = 90, tea 180 x 10% = 18 -> 108.
    # Level 2 (half): rice 45, tea 9 -> 54. Salt earns no one anything.
    rows = await ledger()
    assert set(rows) == {1, 2}
    assert rows[1].amount == Decimal("108.00") and rows[2].amount == Decimal("54.00")
    assert rows[1].order_subtotal == Decimal("1980.00")
    assert sorted(line.amount for line in rows[1].lines) == [Decimal("18.00"), Decimal("90.00")]
    assert {line.product_id for line in rows[1].lines} == {rice, tea}

    # The agent sees products, units and earnings -- never what was spent.
    r = await api.get("/api/v1/partner/commissions", headers=agent)
    entry = next(c for c in _data(r)["commissions"] if c["order_number"] == order["order_number"])
    assert sorted((ln["quantity"], ln["amount"]) for ln in entry["lines"]) == [(1, 18.0), (2, 90.0)]
    assert "basis" not in r.text and "order_subtotal" not in r.text
    r = await api.get("/api/v1/partner/top-products", headers=agent)
    top = _data(r)["products"]
    assert top[0]["product_id"] == str(rice) and top[0]["total_earned"] == 90.0 and top[0]["units"] == 2

    # The header line now speaks in "up to", from the server.
    me = _data(await api.get("/api/v1/partner/me", headers=agent))
    assert me["commissionPercent"] >= 10.0 and "up to" in me["commissionText"]

    # A rate change later never touches this order.
    await api.put(f"{RC}/products/{rice}", json={"percentage": 1}, headers=boss.headers)
    assert (await ledger())[1].amount == Decimal("108.00")

    # One bag of rice comes back: rice keeps 1800 x 1/2 x 5% = 45 at level 1
    # (22.50 at level 2); tea is untouched. 45 + 18 = 63 ; 22.50 + 9 = 31.50.
    r = await api.post(
        f"/api/v1/admin/orders/{order['order_id']}/status/override",
        json={"status": "delivered", "reason": "Delivered and cash collected at the door."},
        headers=boss.headers,
    )
    assert r.status_code == 200, r.text
    db.expire_all()
    row = (await db.execute(
        select(Order).where(Order.order_id == order["order_id"]).options(selectinload(Order.items))
    )).scalar_one()
    rice_item = next(i for i in row.items if i.product_id == rice)
    row.return_items = [{"order_item_id": str(rice_item.order_item_id), "quantity": 1}]
    await OrderService._credit_return_refund(db, row, note="ZZ one bag torn")
    await db.commit()

    # The customer gets back what that bag cost them: 1000 less its 100 of
    # the coupon, not the shelf price.
    db.expire_all()
    refunded = (await db.execute(select(Order.refund_amount).where(Order.order_id == order["order_id"]))).scalar_one()
    assert refunded == Decimal("900.00")
    body = (await api.get("/api/v1/wallet", headers=buyer.headers)).json()
    wallet = body.get("data", body)
    assert float(wallet["balance"]) == 900.0

    rows = await ledger()
    assert rows[1].amount == Decimal("63.00") and rows[2].amount == Decimal("31.50")
    rice_line = next(line for line in rows[1].lines if line.product_id == rice)
    assert rice_line.returned_quantity == 1
    assert rows[1].status == "pending"


async def test_an_order_of_zero_rate_goods_writes_no_commission(
    api, db, world, make_customer, make_product, place_order
):
    from app.models.referral import ReferralCommission

    alice, bob = await make_customer(), await make_customer()
    code = _data(await api.get("/api/v1/referrals/me", headers=alice.headers))["referralCode"]
    assert (await api.post("/api/v1/referrals/redeem", json={"referral_code": code},
                           headers=bob.headers)).status_code == 200
    order = await place_order(bob, await make_product(price="900.00"), 3)

    db.expire_all()
    rows = (await db.execute(select(ReferralCommission).where(
        ReferralCommission.order_id == order["order_id"]))).scalars().all()
    assert rows == []


@pytest.mark.parametrize("bad", [-1, 101, "abc"])
async def test_nonsense_rates_are_refused(api, db, world, make_product, make_admin, bad):
    boss = await make_admin("super_admin", api=api)
    pid = await make_product()
    r = await api.put(f"{RC}/products/{pid}", json={"percentage": bad}, headers=boss.headers)
    assert r.status_code == 422, r.text
