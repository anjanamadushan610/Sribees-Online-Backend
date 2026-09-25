"""
Marketing and the partner programme: coupons (created by marketing, redeemed
at a real checkout), banners, Quick Sale, and the professional referral team's
separate portal.

A coupon is money. The checkout tests here pin that the discount the customer
sees is the discount they get, and that every limit on a coupon actually
holds at checkout -- not only in the admin form.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest


# Passwords are generated per run, never written into the repository: a
# literal one is indistinguishable from a leaked secret to the scanner, and
# the scanner is right to ask. Each satisfies the strength rules.
def _pw() -> str:
    import secrets

    return f"Zz{secrets.token_urlsafe(10)}9!"

PARTNER_PASSWORD = _pw()


def _code(prefix="ZZ") -> str:
    return f"{prefix}{uuid.uuid4().hex[:8].upper()}"


def _window(days_from=-1, days_to=30) -> dict:
    now = datetime.now(timezone.utc)
    return {"valid_from": (now + timedelta(days=days_from)).isoformat(),
            "valid_until": (now + timedelta(days=days_to)).isoformat()}


async def _coupon(api, admin, **over) -> dict:
    body = {"code": _code(), "description": "ZZ test", "discount_type": "percentage", "discount_value": "10",
            **_window(), **over}
    r = await api.post("/api/v1/admin/coupons", json=body, headers=admin.headers)
    assert r.status_code == 201, r.text
    return r.json()["data"]


async def _cart_with(api, customer, product, qty, place_branch):
    await place_branch(customer)
    r = await api.post(
        "/api/v1/cart/items",
        json={"product_id": str(product), "quantity": qty, "price": "1.00", "name": "ZZ"},
        headers=customer.headers,
    )
    assert r.status_code == 200, r.text


@pytest.fixture
def place_branch(app_redis, world):
    import json

    from app.config.redis import RedisKeys

    async def _set(customer):
        await app_redis.setex(
            RedisKeys.branch_context(str(customer.user_id)), 600,
            json.dumps({"branch_id": str(world.branch_id), "branch_name": "ZZ"}),
        )

    return _set


def _order_body(customer) -> dict:
    return {"delivery_address_id": str(customer.address_id), "contact_phone": customer.phone,
            "alternate_phone": "+94719999999", "payment_method": "COD"}


# ---------------------------------------------------------------------------
# Coupons: admin side
# ---------------------------------------------------------------------------


async def test_coupon_admin_lifecycle(api, world, make_admin):
    marketing = await make_admin("marketing_manager", api=api)
    c = await _coupon(api, marketing, code=_code("  zzlow"))
    assert c["code"] == c["code"].upper().strip(), "codes are normalised"
    cid = c["coupon_id"]
    assert str(c.get("branch_id")) == str(world.branch_id), "a scoped creator's coupon is pinned to their branch"

    r = await api.get("/api/v1/admin/coupons", headers=marketing.headers)
    assert r.status_code == 200 and cid in r.text
    r = await api.put(f"/api/v1/admin/coupons/{cid}", json={"discount_value": "15"}, headers=marketing.headers)
    assert r.status_code == 200, r.text

    r = await api.post("/api/v1/admin/coupons", json={"code": c["code"], "discount_value": "5", **_window()},
                       headers=marketing.headers)
    assert r.status_code == 400, "codes are unique"
    r = await api.post("/api/v1/admin/coupons", json={"code": _code(), "discount_value": "5",
                                                      **_window(days_from=5, days_to=1)},
                       headers=marketing.headers)
    assert r.status_code == 422, "valid_until must follow valid_from"
    r = await api.post("/api/v1/admin/coupons", json={"code": _code(), "discount_type": "fixed",
                                                      "discount_value": "500", "min_order_value": "100", **_window()},
                       headers=marketing.headers)
    assert r.status_code == 422, "a fixed discount larger than the minimum order is refused"

    r = await api.delete(f"/api/v1/admin/coupons/{cid}", headers=marketing.headers)
    assert r.status_code == 200, r.text

    manager = await make_admin("inventory_manager", api=api)
    assert (await api.get("/api/v1/admin/coupons", headers=manager.headers)).status_code == 403


# ---------------------------------------------------------------------------
# Coupons at checkout
# ---------------------------------------------------------------------------


async def test_a_coupon_discounts_the_order_exactly(api, world, make_admin, make_customer, make_product,
                                                    place_branch, fake_courier):
    boss = await make_admin("super_admin", api=api)
    coupon = await _coupon(api, boss, discount_value="10", min_order_value="500")
    customer = await make_customer()
    product = await make_product(price="400.00")

    await _cart_with(api, customer, product, 1, place_branch)
    r = await api.post("/api/v1/cart/coupon", json={"code": coupon["code"]}, headers=customer.headers)
    assert r.status_code == 400, "400 is below the 500 minimum"

    await _cart_with(api, customer, product, 1, place_branch)  # now 800
    r = await api.post("/api/v1/cart/coupon", json={"code": coupon["code"].lower()}, headers=customer.headers)
    assert r.status_code == 200, r.text

    r = await api.post("/api/v1/orders/quote", json={"payment_method": "COD",
                                                     "delivery_address_id": str(customer.address_id)},
                       headers=customer.headers)
    quote = r.json()["data"]
    assert quote["discount"] == pytest.approx(80.0)

    r = await api.post("/api/v1/orders", json=_order_body(customer), headers=customer.headers)
    assert r.status_code == 201, r.text
    order = r.json()["data"]
    assert order["discount_amount"] == pytest.approx(80.0)
    assert order["total_amount"] == pytest.approx(quote["total"])
    assert order["coupon_code"] == coupon["code"]


async def test_coupon_limits_hold_at_checkout(api, db, world, make_admin, make_customer, make_product,
                                              place_branch, fake_courier):
    from sqlalchemy import update

    from app.models.coupon import Coupon

    boss = await make_admin("super_admin", api=api)
    once = await _coupon(api, boss, per_user_limit=1)
    customer = await make_customer()
    product = await make_product(price="300.00")

    await _cart_with(api, customer, product, 2, place_branch)
    r = await api.post("/api/v1/cart/coupon", json={"code": once["code"]}, headers=customer.headers)
    assert r.status_code == 200, r.text
    r = await api.post("/api/v1/orders", json=_order_body(customer), headers=customer.headers)
    assert r.status_code == 201, r.text

    await _cart_with(api, customer, product, 2, place_branch)
    r = await api.post("/api/v1/cart/coupon", json={"code": once["code"]}, headers=customer.headers)
    assert r.status_code == 400, "the second use by the same customer is refused"
    await api.delete("/api/v1/cart", headers=customer.headers)

    # Applied while valid, then expired underneath the customer: checkout
    # re-checks the coupon against the database and refuses.
    expiring = await _coupon(api, boss)
    await _cart_with(api, customer, product, 2, place_branch)
    r = await api.post("/api/v1/cart/coupon", json={"code": expiring["code"]}, headers=customer.headers)
    assert r.status_code == 200, r.text
    await db.execute(
        update(Coupon)
        .where(Coupon.code == expiring["code"])
        .values(valid_until=datetime.now(timezone.utc) - timedelta(minutes=1))
    )
    await db.commit()
    r = await api.post("/api/v1/orders", json=_order_body(customer), headers=customer.headers)
    assert r.status_code == 400, "an expired coupon is re-checked at checkout"


async def test_first_order_only_coupons(api, world, make_admin, make_customer, make_product, place_order,
                                        place_branch, fake_courier):
    boss = await make_admin("super_admin", api=api)
    welcome = await _coupon(api, boss, first_order_only=True)
    regular = await make_customer()
    product = await make_product(price="300.00")
    order = await place_order(regular, product, 1)
    # A completed order makes the customer no longer "first order".
    r = await api.post(f"/api/v1/admin/orders/{order['order_id']}/status/override",
                       json={"status": "delivered", "reason": "Delivered to the customer in person."},
                       headers=boss.headers)
    assert r.status_code == 200, r.text

    await _cart_with(api, regular, product, 2, place_branch)
    r = await api.post("/api/v1/cart/coupon", json={"code": welcome["code"]}, headers=regular.headers)
    assert r.status_code == 400, r.text

    newcomer = await make_customer()
    await _cart_with(api, newcomer, product, 2, place_branch)
    r = await api.post("/api/v1/cart/coupon", json={"code": welcome["code"]}, headers=newcomer.headers)
    assert r.status_code == 200, r.text


# ---------------------------------------------------------------------------
# Banners and Quick Sale
# ---------------------------------------------------------------------------


async def test_banners(api, world, make_admin):
    marketing = await make_admin("marketing_manager", api=api)
    r = await api.post("/api/v1/admin/banners",
                       json={"title": "ZZ Avurudu Sale", "image_url": "/uploads/banners/zz.png", "sort_order": 1},
                       headers=marketing.headers)
    assert r.status_code == 201, r.text
    bid = r.json()["data"]["banner_id"]
    r = await api.get("/api/v1/admin/banners", headers=marketing.headers)
    assert r.status_code == 200 and bid in r.text
    r = await api.put(f"/api/v1/admin/banners/{bid}", json={"title": "ZZ Renamed"}, headers=marketing.headers)
    assert r.status_code == 200, r.text
    r = await api.delete(f"/api/v1/admin/banners/{bid}", headers=marketing.headers)
    assert r.status_code == 200, r.text
    support = await make_admin("customer_support", api=api)
    r = await api.post("/api/v1/admin/banners", json={"title": "x"}, headers=support.headers)
    assert r.status_code == 403


async def test_quick_sale_and_the_marketing_dashboard(api, db, world, make_admin, make_product):
    marketing = await make_admin("marketing_manager", api=api)
    product = await make_product(price="1000.00")
    r = await api.get("/api/v1/admin/marketing/products", params={"search": "ZZ"}, headers=marketing.headers)
    assert r.status_code == 200, r.text
    r = await api.patch(f"/api/v1/admin/marketing/inventory/{product}",
                        json={"discount_percentage": 25, "is_on_sale": True}, headers=marketing.headers)
    assert r.status_code == 200, r.text
    r = await api.get("/api/v1/admin/marketing/quick-sale", headers=marketing.headers)
    assert r.status_code == 200 and str(product) in r.text
    r = await api.get("/api/v1/admin/marketing/dashboard", headers=marketing.headers)
    assert r.status_code == 200, r.text


# ---------------------------------------------------------------------------
# Partners: the referral team and its own portal
# ---------------------------------------------------------------------------


async def test_the_partner_programme(api, world, make_admin):
    boss = await make_admin("super_admin", api=api)
    email = f"zz.partner.{uuid.uuid4().hex[:8]}@sribees-ci.lk"
    r = await api.post("/api/v1/admin/partners",
                       json={"email": email, "password": PARTNER_PASSWORD, "full_name": "ZZ Partner"},
                       headers=boss.headers)
    assert r.status_code == 201, r.text
    pid = r.json()["data"]["partner_id"]

    r = await api.post("/api/v1/partner/auth/login", json={"email": email, "password": PARTNER_PASSWORD},
                       headers={"X-Forwarded-For": f"192.0.2.{uuid.uuid4().int % 250}"})
    assert r.status_code == 200, r.text
    token = r.json()["data"]["token"]
    ph = {"Authorization": f"Bearer {token}"}
    for path in ["/api/v1/partner/me", "/api/v1/partner/customers", "/api/v1/partner/recruits",
                 "/api/v1/partner/commissions"]:
        r = await api.get(path, headers=ph)
        assert r.status_code == 200, (path, r.text)

    # A partner token is worthless on the admin side.
    assert (await api.get("/api/v1/admin/orders", headers=ph)).status_code in (401, 403)

    r = await api.get(f"/api/v1/admin/partners/{pid}/commissions", headers=boss.headers)
    assert r.status_code == 200, r.text
    r = await api.post(f"/api/v1/admin/partners/{pid}/commissions/mark-paid", json={}, headers=boss.headers)
    assert r.status_code == 200, r.text

    r = await api.patch(f"/api/v1/admin/partners/{pid}", json={"is_active": False}, headers=boss.headers)
    assert r.status_code == 200, r.text
    assert (await api.get("/api/v1/partner/me", headers=ph)).status_code in (401, 403)

    manager = await make_admin("branch_manager", api=api)
    assert (await api.get("/api/v1/admin/partners", headers=manager.headers)).status_code == 403
