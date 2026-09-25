"""
The money path, end to end: browse -> cart -> quote -> order -> cancel.

This is the regression test for the thing the business is: a customer buys
groceries, is charged the right amount, the branch's stock moves, and a
cancellation puts it back. Every step goes through HTTP into the real app,
Postgres and Redis. Only SribeesExpress is faked (at the network).
"""
from __future__ import annotations

import json
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.config.redis import RedisKeys


async def _set_branch(app_redis, customer, world):
    await app_redis.setex(
        RedisKeys.branch_context(str(customer.user_id)),
        600,
        json.dumps({"branch_id": str(world.branch_id), "branch_name": "ZZ"}),
    )


async def _stock(db, product_id, branch_id) -> int:
    from app.models.product import BranchInventory

    db.expire_all()
    return (
        await db.execute(
            select(BranchInventory.stock_quantity).where(
                BranchInventory.product_id == product_id,
                BranchInventory.branch_id == branch_id,
            )
        )
    ).scalar_one()


def _order_body(customer, **extra) -> dict:
    return {
        "delivery_address_id": str(customer.address_id),
        "contact_phone": customer.phone,
        "alternate_phone": "+94719999999",
        "payment_method": "COD",
        **extra,
    }


async def _add(api, customer, product_id, *, qty=1, claimed_price="1.00"):
    return await api.post(
        "/api/v1/cart/items",
        json={
            "product_id": str(product_id),
            "quantity": qty,
            "price": claimed_price,
            "name": "ZZ Test Rice",
        },
        headers=customer.headers,
    )


async def test_the_full_cod_journey(api, db, app_redis, world, make_customer, make_product, fake_courier):
    customer = await make_customer()
    product = await make_product(price="250.00", stock=20)
    await _set_branch(app_redis, customer, world)

    # --- browse: the branch shows the product at its catalogue price --------
    r = await api.get(f"/api/v1/products/{product}", headers=customer.headers)
    assert r.status_code == 200, r.text

    # --- cart: whatever price the client claims, the cart shows the real one
    r = await _add(api, customer, product, qty=2, claimed_price="1.00")
    assert r.status_code == 200, r.text
    r = await api.get("/api/v1/cart", headers=customer.headers)
    assert r.status_code == 200, r.text
    cart = r.json()["data"]
    assert cart["totals"]["subtotal"] == pytest.approx(500.0), cart["totals"]

    # --- quote: server-authoritative, includes the courier's fee -------------
    r = await api.post(
        "/api/v1/orders/quote",
        json={"payment_method": "COD", "delivery_address_id": str(customer.address_id)},
        headers=customer.headers,
    )
    assert r.status_code == 200, r.text
    quote = r.json()["data"]
    assert quote["subtotal"] == pytest.approx(500.0)
    assert quote["delivery_fee"] == pytest.approx(350.0)
    quoted_total = quote["total"]

    # --- order -----------------------------------------------------------------
    r = await api.post("/api/v1/orders", json=_order_body(customer), headers=customer.headers)
    assert r.status_code == 201, r.text
    order = r.json()["data"]
    assert order["status"] == "pending"
    assert order["payment_method"].upper() == "COD"
    assert [i["unit_price"] for i in order["items"]] == [pytest.approx(250.0)]
    assert order["subtotal"] == pytest.approx(500.0)
    assert order["total_amount"] == pytest.approx(quoted_total), "charged must equal quoted"
    assert await _stock(db, product, world.branch_id) == 18
    assert any("/confirm" in p for p in fake_courier.paths()), "the courier price must be held"

    # --- the cart is emptied by a successful order ---------------------------
    r = await api.get("/api/v1/cart", headers=customer.headers)
    assert r.json()["data"]["items"] == []

    # --- the customer sees it -------------------------------------------------
    r = await api.get("/api/v1/orders", headers=customer.headers)
    assert r.status_code == 200, r.text
    assert order["order_id"] in json.dumps(r.json())
    r = await api.get(f"/api/v1/orders/{order['order_id']}", headers=customer.headers)
    assert r.status_code == 200, r.text

    # --- cancel puts the stock back -------------------------------------------
    r = await api.post(
        f"/api/v1/orders/{order['order_id']}/cancel?reason=changed+my+mind",
        headers=customer.headers,
    )
    assert r.status_code == 200, r.text
    assert r.json()["data"]["status"] == "cancelled"
    assert await _stock(db, product, world.branch_id) == 20


async def test_a_client_supplied_price_is_never_charged(api, db, app_redis, world, make_customer, make_product, fake_courier):
    """SEC-002: both routes by which a client could name its own price."""
    customer = await make_customer()
    product = await make_product(price="1200.00", stock=5)
    await _set_branch(app_redis, customer, world)

    # 1. Buy now: the lines come in the request body.
    r = await api.post(
        "/api/v1/orders",
        json=_order_body(
            customer,
            is_buy_now=True,
            items=[{"product_id": str(product), "quantity": 1, "price": 0.01, "name": "x"}],
        ),
        headers=customer.headers,
    )
    assert r.status_code == 201, r.text
    assert r.json()["data"]["items"][0]["unit_price"] == pytest.approx(1200.0)
    assert r.json()["data"]["subtotal"] == pytest.approx(1200.0)

    # 2. Cart: the price was stored in Redis when the item was added.
    r = await _add(api, customer, product, qty=1, claimed_price="0.01")
    assert r.status_code == 200, r.text
    r = await api.post("/api/v1/orders", json=_order_body(customer), headers=customer.headers)
    assert r.status_code == 201, r.text
    assert r.json()["data"]["subtotal"] == pytest.approx(1200.0)


async def test_branch_price_and_discount_are_what_is_charged(api, app_redis, world, make_customer, make_product, fake_courier):
    customer = await make_customer()
    # Global 500, branch override 400, 10% off -> 360.00
    product = await make_product(price="500.00", branch_price="400.00", discount=10.0)
    await _set_branch(app_redis, customer, world)
    await _add(api, customer, product, qty=1, claimed_price="500.00")
    r = await api.post("/api/v1/orders", json=_order_body(customer), headers=customer.headers)
    assert r.status_code == 201, r.text
    assert Decimal(str(r.json()["data"]["items"][0]["unit_price"])) == Decimal("360.00")


async def test_a_deactivated_product_cannot_be_bought(api, app_redis, world, make_customer, make_product, fake_courier):
    customer = await make_customer()
    product = await make_product(active=False)
    await _set_branch(app_redis, customer, world)
    r = await api.post(
        "/api/v1/orders",
        json=_order_body(
            customer, is_buy_now=True, items=[{"product_id": str(product), "quantity": 1, "price": 1, "name": "Gone"}]
        ),
        headers=customer.headers,
    )
    assert r.status_code == 400, r.text
    assert "no longer available" in r.text


async def test_you_cannot_order_more_than_the_branch_has(api, app_redis, world, make_customer, make_product, fake_courier):
    customer = await make_customer()
    product = await make_product(stock=2)
    await _set_branch(app_redis, customer, world)
    r = await _add(api, customer, product, qty=3, claimed_price="250.00")
    assert r.status_code == 400, r.text
    assert "Only 2" in r.text


async def test_an_unserviceable_address_is_refused_not_charged_a_made_up_fee(
    api, app_redis, world, make_customer, make_product, fake_courier
):
    fake_courier.serviceable = False
    customer = await make_customer()
    product = await make_product()
    await _set_branch(app_redis, customer, world)
    await _add(api, customer, product)
    r = await api.post("/api/v1/orders", json=_order_body(customer), headers=customer.headers)
    assert r.status_code in {400, 422}, r.text


async def test_orders_are_private_to_their_customer(api, app_redis, world, make_customer, make_product, fake_courier):
    """IDOR: another customer's order id must look exactly like a missing one."""
    alice = await make_customer()
    mallory = await make_customer()
    product = await make_product()
    await _set_branch(app_redis, alice, world)
    await _add(api, alice, product)
    r = await api.post("/api/v1/orders", json=_order_body(alice), headers=alice.headers)
    assert r.status_code == 201, r.text
    order_id = r.json()["data"]["order_id"]

    for method, path in [
        ("GET", f"/api/v1/orders/{order_id}"),
        ("GET", f"/api/v1/orders/{order_id}/tracking"),
        ("GET", f"/api/v1/orders/{order_id}/invoice"),
        ("POST", f"/api/v1/orders/{order_id}/cancel"),
        ("POST", f"/api/v1/orders/{order_id}/reorder"),
    ]:
        r = await api.request(method, path, headers=mallory.headers)
        # 400 "Order not found" from /reorder is as good as a 404: the point
        # is that nothing of Alice's order comes back.
        assert r.status_code in {400, 403, 404}, f"{method} {path} -> {r.status_code}: {r.text[:200]}"
        assert alice.phone not in r.text and "12 Test Lane" not in r.text

    # and using her address for his order is refused too
    r = await api.post(
        "/api/v1/orders",
        json={**_order_body(mallory), "delivery_address_id": str(alice.address_id)},
        headers=mallory.headers,
    )
    assert r.status_code == 400, r.text


async def _place(api, app_redis, world, customer, product, qty=2):
    await _set_branch(app_redis, customer, world)
    r = await _add(api, customer, product, qty=qty, claimed_price="250.00")
    assert r.status_code == 200, r.text
    r = await api.post("/api/v1/orders", json=_order_body(customer), headers=customer.headers)
    assert r.status_code == 201, r.text
    return r.json()["data"]["order_id"]


async def test_an_admin_cancellation_also_returns_the_stock(
    api, db, app_redis, world, make_customer, make_product, make_admin, fake_courier
):
    customer = await make_customer()
    product = await make_product(stock=10)
    order_id = await _place(api, app_redis, world, customer, product, qty=3)
    assert await _stock(db, product, world.branch_id) == 7

    admin = await make_admin("super_admin", api=api)
    r = await api.post(
        f"/api/v1/admin/orders/{order_id}/status/override",
        json={"status": "cancelled", "reason": "The branch is out of stock for this order."},
        headers=admin.headers,
    )
    assert r.status_code == 200, r.text
    assert await _stock(db, product, world.branch_id) == 10

    # The override is on the permanent audit trail, marked as one.
    from app.models.order import OrderStatusHistory

    notes = (
        await db.execute(
            select(OrderStatusHistory.notes).where(OrderStatusHistory.order_id == order_id)
        )
    ).scalars().all()
    assert any(n and n.startswith("[EMERGENCY OVERRIDE]") for n in notes), notes


async def test_a_second_cancellation_does_not_restock_twice(
    api, db, app_redis, world, make_customer, make_product, make_admin, fake_courier
):
    customer = await make_customer()
    product = await make_product(stock=10)
    order_id = await _place(api, app_redis, world, customer, product, qty=4)
    r = await api.post(f"/api/v1/orders/{order_id}/cancel", headers=customer.headers)
    assert r.status_code == 200, r.text
    r = await api.post(f"/api/v1/orders/{order_id}/cancel", headers=customer.headers)
    assert r.status_code in {400, 409}, r.text
    admin = await make_admin("super_admin", api=api)
    await api.post(
        f"/api/v1/admin/orders/{order_id}/status/override",
        json={"status": "cancelled", "reason": "A second cancellation attempt."},
        headers=admin.headers,
    )
    assert await _stock(db, product, world.branch_id) == 10


async def test_another_branchs_manager_cannot_touch_the_order(
    api, db, app_redis, world, make_customer, make_product, make_admin, fake_courier
):
    import uuid as _uuid

    from app.models.branch import Branch

    customer = await make_customer()
    product = await make_product()
    order_id = await _place(api, app_redis, world, customer, product, qty=1)

    other = _uuid.uuid4()
    db.add(Branch(branch_id=other, name="ZZ Other", code=f"ZZX{world.suffix}", province=world.province,
                  district=world.district, is_active=True, delivery_runs=[]))
    await db.commit()
    manager = await make_admin("branch_manager", branch_id=other, api=api)

    for method, path, body in [
        ("GET", f"/api/v1/admin/orders/{order_id}", None),
        ("PATCH", f"/api/v1/admin/orders/{order_id}/status", {"status": "cancelled"}),
    ]:
        r = await api.request(method, path, json=body, headers=manager.headers)
        assert r.status_code in {403, 404}, f"{method} {path} -> {r.status_code}: {r.text[:200]}"
    assert await _stock(db, product, world.branch_id) == 19
