"""
An order paid online waits for its money (app/payments).

It is created "awaiting payment" -- not placed, nothing sent, stock held --
and becomes an order only when the gateway confirms the money. A declined
payment can be tried again from the order, by any online method, until the
hold closes; after that the order is cancelled and what it held comes back.
Money that arrives for an order that no longer wants it goes straight back to
the customer's wallet.

The gateway is faked at the network (tests/integration/fake_marx.py).
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.config.redis import RedisKeys

RETURN_URL = "https://api.sribees.test/api/v1/payments/marx/return"


@pytest.fixture
def marx(monkeypatch):
    from app.config.settings import settings
    from tests.integration.fake_marx import install

    state = install(monkeypatch)
    monkeypatch.setattr(settings, "marx_ipg_return_url", RETURN_URL)
    return state


async def _place(api, app_redis, world, customer, product, qty=1, method="MARX_OTHER", lang="en"):
    r = await api.patch("/api/v1/auth/me", json={"email": f"zz.{uuid.uuid4().hex[:8]}@sribees-ci.lk"},
                        headers=customer.headers)
    assert r.status_code == 200, r.text
    await app_redis.setex(RedisKeys.branch_context(str(customer.user_id)), 600,
                          json.dumps({"branch_id": str(world.branch_id), "branch_name": "ZZ"}))
    await api.post("/api/v1/cart/items", json={"product_id": str(product), "quantity": qty, "price": "1",
                                               "name": "ZZ"}, headers=customer.headers)
    return await api.post("/api/v1/orders", json={
        "delivery_address_id": str(customer.address_id), "contact_phone": customer.phone,
        "alternate_phone": "+94719999999", "payment_method": method},
        headers={**customer.headers, "Accept-Language": lang})


def _came_back(action, tr_id, reference):
    return f"{action['return_url']}?trId={tr_id}&merchantRID={reference}"


async def _stock(db, world, product) -> int:
    from app.models.product import BranchInventory

    db.expire_all()
    return (await db.execute(select(BranchInventory.stock_quantity).where(
        BranchInventory.branch_id == world.branch_id, BranchInventory.product_id == product))).scalar_one()


async def _placed_notices(db, customer) -> int:
    from app.models.notification import Notification

    db.expire_all()
    rows = (await db.execute(select(Notification.data).where(Notification.user_id == customer.user_id))).scalars()
    return sum(1 for d in rows if (d or {}).get("status") in ("awaiting_payment", "confirmed", "pending"))


async def test_an_online_order_is_placed_only_when_the_money_arrives(
        api, db, app_redis, world, make_customer, make_product, fake_courier, marx):
    customer = await make_customer()
    product = await make_product(price="500.00", stock=10)
    r = await _place(api, app_redis, world, customer, product, qty=2)
    assert r.status_code == 201, r.text
    order = r.json()["data"]

    assert order["status"] == "awaiting_payment"
    assert order["status_label"] == "Payment processing"
    assert order["payment"]["state"] == "processing" and order["payment"]["can_pay"] is True
    action = order["payment_action"]
    assert action["type"] == "redirect" and action["url"].startswith("https://pay.marx.test/")
    assert action["return_url"] == RETURN_URL, "the app learns where the gateway returns from us"
    assert action["complete_path"] == f"/payments/orders/{order['order_id']}/complete"
    assert await _stock(db, world, product) == 8, "the stock is held while they pay"
    assert await _placed_notices(db, customer) == 0, "nothing says 'placed' before the money"

    tr_id = action["url"].rsplit("/", 1)[1]
    r = await api.post(action["complete_path"].replace("/payments", "/api/v1/payments", 1),
                       json={"return_url": _came_back(action, tr_id, order["order_number"])},
                       headers=customer.headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["success"] is True and body["data"]["outcome"] == "paid"
    assert body["data"]["status"] == "confirmed" and body["data"]["payment_status"] == "paid"
    assert body["data"]["payment"]["state"] == "paid"
    assert await _placed_notices(db, customer) == 1, "placed, once, now that it is paid"


async def test_a_declined_payment_can_be_paid_again_from_the_order(
        api, db, app_redis, world, make_customer, make_product, fake_courier, marx):
    customer = await make_customer()
    r = await _place(api, app_redis, world, customer, await make_product(price="300.00"))
    order = r.json()["data"]
    action = order["payment_action"]
    first_tr = action["url"].rsplit("/", 1)[1]

    marx.outcome = "FAILURE"
    r = await api.post(f"/api/v1/payments/orders/{order['order_id']}/complete",
                       json={"return_url": _came_back(action, first_tr, order["order_number"])},
                       headers=customer.headers)
    assert r.status_code == 200, r.text
    assert r.json()["success"] is False and r.json()["data"]["outcome"] == "failed"

    r = await api.get(f"/api/v1/orders/{order['order_id']}", headers={**customer.headers, "Accept-Language": "si"})
    shown = r.json()["data"]
    assert shown["status"] == "awaiting_payment"
    assert shown["status_label"] == "ගෙවීම අසාර්ථකයි", "in the customer's language"
    assert shown["payment"]["state"] == "failed" and shown["payment"]["can_pay"] is True
    assert shown["payment"]["tone"] == "error"

    # Again, by bank transfer this time.
    marx.outcome = "SUCCESS"
    r = await api.post(f"/api/v1/payments/orders/{order['order_id']}/pay", json={"method": "MARX_BANK"},
                       headers=customer.headers)
    assert r.status_code == 200, r.text
    retry = r.json()["data"]["payment_action"]
    second_tr = retry["url"].rsplit("/", 1)[1]
    assert marx.orders[second_tr]["merchantRID"] == f"{order['order_number']}-2"
    assert marx.orders[second_tr]["paymentMethod"] == "PAY_BY_BANK_ACCOUNT"

    # The declined try's return can't be replayed onto the new one.
    r = await api.post(f"/api/v1/payments/orders/{order['order_id']}/complete",
                       json={"return_url": _came_back(retry, first_tr, f"{order['order_number']}-2")},
                       headers=customer.headers)
    assert r.status_code == 400

    r = await api.post(f"/api/v1/payments/orders/{order['order_id']}/complete",
                       json={"return_url": _came_back(retry, second_tr, f"{order['order_number']}-2")},
                       headers=customer.headers)
    assert r.status_code == 200 and r.json()["data"]["status"] == "confirmed"
    assert r.json()["data"]["payment_method"] == "MARX_BANK"

    r = await api.post(f"/api/v1/payments/orders/{order['order_id']}/pay", headers=customer.headers)
    assert r.status_code == 400, "a paid order is not paid again"


async def test_only_the_owner_pays_and_only_through_their_own_try(
        api, app_redis, world, make_customer, make_product, fake_courier, marx):
    customer = await make_customer()
    order = (await _place(api, app_redis, world, customer, await make_product())).json()["data"]
    action = order["payment_action"]
    tr_id = action["url"].rsplit("/", 1)[1]

    stranger = await make_customer()
    for path, body in (("pay", {}), ("complete", {"return_url": _came_back(action, tr_id, order["order_number"])})):
        r = await api.post(f"/api/v1/payments/orders/{order['order_id']}/{path}", json=body,
                           headers=stranger.headers)
        assert r.status_code == 404, path

    r = await api.post(f"/api/v1/payments/orders/{order['order_id']}/complete",
                       json={"return_url": _came_back(action, "TRFORGED", order["order_number"])},
                       headers=customer.headers)
    assert r.status_code == 400 and marx.captures == [], "a forged return captures nothing"

    r = await api.post(f"/api/v1/payments/orders/{order['order_id']}/pay", json={"method": "COD"},
                       headers=customer.headers)
    assert r.status_code == 400, "paying again is online only"


async def test_an_unpaid_order_is_cancelled_when_its_window_closes(
        api, db, app_redis, world, make_customer, make_product, fake_courier, marx):
    from app.config.settings import settings
    from app.models.order import Order
    from app.payments import service

    customer = await make_customer()
    product = await make_product(stock=5)
    order = (await _place(api, app_redis, world, customer, product, qty=2)).json()["data"]
    assert await _stock(db, world, product) == 3

    later = datetime.now(timezone.utc) + timedelta(
        minutes=settings.payment_hold_minutes + settings.payment_attempt_grace_minutes + 1)
    result = await service.sweep(db, now=later)
    assert result["expired_orders"] >= 1

    db.expire_all()
    row = (await db.execute(select(Order).where(Order.order_id == uuid.UUID(order["order_id"])))).scalar_one()
    assert row.status == "cancelled"
    assert await _stock(db, world, product) == 5, "the held stock is back on the shelf"

    r = await api.get(f"/api/v1/orders/{order['order_id']}", headers=customer.headers)
    assert r.json()["data"]["payment"]["state"] == "not_completed"
    r = await api.post(f"/api/v1/payments/orders/{order['order_id']}/pay", headers=customer.headers)
    assert r.status_code == 400, "a cancelled order cannot be paid"


async def test_money_that_arrives_late_or_twice_goes_back_to_the_wallet(
        api, db, app_redis, world, make_customer, make_product, fake_courier, marx, monkeypatch):
    from app.config.settings import settings
    from app.models.wallet import WalletTransaction
    from app.payments import service
    from app.services.marx_ipg_service import MarxIPGService

    paid_in_the_background: set[str] = set()

    async def summary(*, tr_id: str) -> dict:
        if tr_id in paid_in_the_background:
            return {"summary_result": "SUCCESS", "order_status": "CAPTURED", "gateway_code": "00", "raw": {}}
        return {"summary_result": "PENDING", "raw": {}}

    monkeypatch.setattr(MarxIPGService, "get_order_summary", staticmethod(summary))

    # Twice: they opened a second try, paid it, and the first went through too.
    customer = await make_customer()
    order = (await _place(api, app_redis, world, customer, await make_product(price="400.00"))).json()["data"]
    first_tr = order["payment_action"]["url"].rsplit("/", 1)[1]
    retry = (await api.post(f"/api/v1/payments/orders/{order['order_id']}/pay",
                            headers=customer.headers)).json()["data"]["payment_action"]
    second_tr = retry["url"].rsplit("/", 1)[1]
    r = await api.post(f"/api/v1/payments/orders/{order['order_id']}/complete",
                       json={"return_url": _came_back(retry, second_tr, f"{order['order_number']}-2")},
                       headers=customer.headers)
    assert r.json()["data"]["status"] == "confirmed"

    paid_in_the_background.add(first_tr)
    await service.sweep(db, now=datetime.now(timezone.utc) + timedelta(minutes=3))
    db.expire_all()
    refunds = (await db.execute(select(WalletTransaction.amount).where(
        WalletTransaction.user_id == customer.user_id, WalletTransaction.title == "Payment refund"))).scalars().all()
    assert [float(a) for a in refunds] == [float(order["total_amount"])], "the second payment came back"

    # Late: the window closed, the order was cancelled, then the money came.
    late = await make_customer()
    order = (await _place(api, app_redis, world, late, await make_product(price="250.00"))).json()["data"]
    tr_id = order["payment_action"]["url"].rsplit("/", 1)[1]
    closed = datetime.now(timezone.utc) + timedelta(
        minutes=settings.payment_hold_minutes + settings.payment_attempt_grace_minutes + 1)
    await service.sweep(db, now=closed)
    paid_in_the_background.add(tr_id)
    await service.sweep(db, now=closed + timedelta(minutes=5))

    r = await api.get(f"/api/v1/orders/{order['order_id']}", headers=late.headers)
    assert r.json()["data"]["status"] == "cancelled"
    assert r.json()["data"]["payment_status"] == "refunded"
    db.expire_all()
    refunds = (await db.execute(select(WalletTransaction.amount).where(
        WalletTransaction.user_id == late.user_id, WalletTransaction.title == "Payment refund"))).scalars().all()
    assert [float(a) for a in refunds] == [float(order["total_amount"])]


async def test_methods_come_from_the_server_in_english(
        api, app_redis, world, make_customer, make_product, fake_courier, marx, monkeypatch):
    from app.config.settings import settings

    customer = await make_customer()
    r = await api.get("/api/v1/payment-methods", headers={**customer.headers, "Accept-Language": "si"})
    methods = {m["code"]: m for m in r.json()["data"]["methods"]}
    assert methods["MARX_BANK"]["name"] == "Bank Transfer", "English whatever the app's language"
    assert methods["MARX_BANK"]["description"] == "Pay straight from your bank account"
    assert methods["MARX_BANK"]["icon"] == "bank" and methods["MARX_BANK"]["kind"] == "online"
    assert methods["COD"]["kind"] == "offline" and methods["COD"]["type"] == "OFFLINE", "old apps still read type"

    monkeypatch.setattr(settings, "marx_gateway_methods_enabled", False)
    r = await api.get("/api/v1/payment-methods", headers=customer.headers)
    assert "MARX" not in r.text, "a switched-off gateway is not offered"
    r = await _place(api, app_redis, world, customer, await make_product())
    assert r.status_code == 400, "nor accepted if a client sends it anyway"
