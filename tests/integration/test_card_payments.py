"""
Paying by card through Marx IPG: the order is created unpaid with a hosted
payment page, and only a capture the gateway confirms -- for this order's own
transaction -- marks it paid.

The gateway is faked at the network (tests/integration/fake_marx.py), so
MarxIPGService's own request building and parsing run.
"""
from __future__ import annotations

import json

import pytest

from app.config.redis import RedisKeys


@pytest.fixture
def marx(monkeypatch):
    from tests.integration.fake_marx import install

    return install(monkeypatch)


async def _card_order(api, app_redis, world, customer, product, qty=1):
    # Marx needs an email for the receipt; the app asks for one before a card.
    import uuid as _uuid

    r = await api.patch("/api/v1/auth/me", json={"email": f"zz.{_uuid.uuid4().hex[:8]}@sribees-ci.lk"},
                        headers=customer.headers)
    assert r.status_code == 200, r.text
    await app_redis.setex(RedisKeys.branch_context(str(customer.user_id)), 600,
                          json.dumps({"branch_id": str(world.branch_id), "branch_name": "ZZ"}))
    await api.post("/api/v1/cart/items", json={"product_id": str(product), "quantity": qty, "price": "1",
                                               "name": "ZZ"}, headers=customer.headers)
    return await api.post("/api/v1/orders", json={
        "delivery_address_id": str(customer.address_id), "contact_phone": customer.phone,
        "alternate_phone": "+94719999999", "payment_method": "MARX_OTHER"}, headers=customer.headers)


def _confirm_body(order, tr_id=None, rid=None):
    return {"order_id": order["order_id"], "tr_id": tr_id or order["_tr_id"],
            "merchant_rid": rid or order["order_number"]}


async def test_a_card_payment_confirmed_by_the_gateway(api, app_redis, world, make_customer, make_product,
                                                      fake_courier, marx):
    customer = await make_customer()
    r = await _card_order(api, app_redis, world, customer, await make_product(price="600.00"), 2)
    assert r.status_code == 201, r.text
    order = r.json()["data"]
    assert order["payment_status"] == "pending"
    assert order["payment_redirect_url"].startswith("https://pay.marx.test/")
    tr_id = order["payment_redirect_url"].rsplit("/", 1)[1]
    sent = marx.orders[tr_id]
    assert sent["merchantRID"] == order["order_number"]
    assert sent["amount"] == pytest.approx(float(order["total_amount"])), "the gateway charges what we quoted"

    order["_tr_id"] = tr_id
    # Someone else's transaction id against this order: refused, nothing captured.
    r = await api.post("/api/v1/payments/marx/confirm", json=_confirm_body(order, tr_id="TRFORGED"),
                       headers=customer.headers)
    assert r.status_code == 400 and marx.captures == []

    other = await make_customer()
    r = await api.post("/api/v1/payments/marx/confirm", json=_confirm_body(order), headers=other.headers)
    assert r.status_code == 404, "a stranger cannot confirm my order"

    r = await api.post("/api/v1/payments/marx/confirm", json=_confirm_body(order), headers=customer.headers)
    assert r.status_code == 200, r.text
    assert r.json()["data"]["payment_status"] == "paid"
    r = await api.post("/api/v1/payments/marx/confirm", json=_confirm_body(order), headers=customer.headers)
    assert r.status_code == 200 and "Already" in r.text
    assert marx.captures == [tr_id], "confirming twice captures once"


async def test_a_declined_card_leaves_the_order_unpaid(api, app_redis, world, make_customer, make_product,
                                                       fake_courier, marx):
    customer = await make_customer()
    r = await _card_order(api, app_redis, world, customer, await make_product(price="300.00"))
    order = r.json()["data"]
    order["_tr_id"] = order["payment_redirect_url"].rsplit("/", 1)[1]
    marx.outcome = "FAILURE"
    r = await api.post("/api/v1/payments/marx/confirm", json=_confirm_body(order), headers=customer.headers)
    assert r.status_code == 200, r.text
    r = await api.get(f"/api/v1/orders/{order['order_id']}", headers=customer.headers)
    assert r.json()["data"]["payment_status"] == "failed"


async def test_when_the_gateway_is_down_no_order_is_created(api, db, app_redis, world, make_customer,
                                                            make_product, fake_courier, marx):
    from sqlalchemy import func, select

    from app.models.order import Order

    customer = await make_customer()
    product = await make_product(price="300.00", stock=7)
    marx.outcome = "DOWN"
    r = await _card_order(api, app_redis, world, customer, product)
    assert r.status_code == 400, r.text
    assert "payment" in r.text.lower(), r.text
    count = (await db.execute(select(func.count()).select_from(Order).where(Order.user_id == customer.user_id)))
    assert count.scalar_one() == 0, "a payment that could not start leaves no half-made order"


async def test_payment_methods_listing(api, world, make_customer, marx):
    customer = await make_customer()
    r = await api.get("/api/v1/payments/methods", headers=customer.headers)
    if r.status_code == 404:
        r = await api.get("/api/v1/payment-methods", headers=customer.headers)
    assert r.status_code == 200, r.text
    assert "COD" in r.text and "MARX" in r.text
