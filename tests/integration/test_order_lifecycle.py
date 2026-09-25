"""
An order after checkout: the courier carries it (signed webhooks move it
along), the customer tracks it, gets an invoice, reorders it, and -- if the
goods were wrong -- returns it and is refunded.

Webhooks are signed exactly as SribeesExpress signs them (HMAC-SHA256 over
"{t}.{body}"), so the signature check, replay window and event dedupe all run
for real.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
import uuid

import pytest

WEBHOOK_SECRET = "whsec_integration_" + uuid.uuid4().hex
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII="
)


@pytest.fixture(autouse=True)
def _webhook_secret(monkeypatch):
    from app.config.settings import settings

    monkeypatch.setattr(settings, "courier_webhook_secret", WEBHOOK_SECRET)


def _balance(r) -> float:
    body = r.json()
    return float(body.get("data", body)["balance"])


def _signed(payload: dict, secret: str = WEBHOOK_SECRET, t: int | None = None) -> tuple[bytes, dict]:
    body = json.dumps(payload).encode()
    t = int(time.time()) if t is None else t
    sig = hmac.new(secret.encode(), f"{t}.".encode() + body, hashlib.sha256).hexdigest()
    return body, {"X-Sribees-Signature": f"t={t},v1={sig}", "Content-Type": "application/json"}


async def _push(api, shipment_id: str, status: str, **extra):
    payload = {"event_id": f"evt_{uuid.uuid4().hex}", "event_type": "shipment.status_changed",
               "shipment_id": shipment_id, "status": status, **extra}
    body, headers = _signed(payload)
    return await api.post("/api/v1/courier/webhooks/shipment-status", content=body, headers=headers), payload


async def _dispatched(api, make_customer, make_product, place_order, make_admin, fake_courier, qty=2):
    """An order packed and booked with the courier; returns (customer, order, shipment_id, manager)."""
    customer = await make_customer()
    product = await make_product(price="400.00", stock=40)
    order = await place_order(customer, product, qty)
    manager = await make_admin("branch_manager", api=api)
    for step in ["confirmed", "packing", "packed"]:
        r = await api.patch(f"/api/v1/admin/orders/{order['order_id']}/status", json={"status": step},
                            headers=manager.headers)
        assert r.status_code == 200, r.text
    r = await api.post(f"/api/v1/admin/orders/{order['order_id']}/courier/request-pickup", headers=manager.headers)
    assert r.status_code == 200, r.text
    # The shipment the courier actually booked for this order.
    shipment_id = next(
        sid for sid, s in fake_courier.shipments.items() if order["order_number"] in s["merchant_order_ref"]
    )
    return customer, order, shipment_id, manager, product


async def _status(api, customer, order_id) -> dict:
    r = await api.get(f"/api/v1/orders/{order_id}", headers=customer.headers)
    assert r.status_code == 200, r.text
    return r.json()["data"]


# ---------------------------------------------------------------------------
# The courier moves the order
# ---------------------------------------------------------------------------


async def test_courier_webhooks_drive_the_order_to_delivered(
    api, world, make_customer, make_product, place_order, make_admin, fake_courier
):
    customer, order, sid, _, _ = await _dispatched(api, make_customer, make_product, place_order, make_admin, fake_courier)

    for courier_status, ours in [("picked_up", "handed_to_courier"), ("in_transit", "shipped"),
                                 ("out_for_delivery", "out_for_delivery")]:
        r, _ = await _push(api, sid, courier_status)
        assert r.status_code == 200 and r.json()["matched"] is True, r.text
        assert (await _status(api, customer, order["order_id"]))["status"] == ours

    r, payload = await _push(api, sid, "delivered",
                             cod={"collected": True, "amount_lkr": order["total_amount"]})
    assert r.status_code == 200, r.text
    delivered = await _status(api, customer, order["order_id"])
    assert delivered["status"] == "delivered"
    assert delivered["payment_status"] == "paid", "cash collected at the door marks a COD order paid"

    # The same event again is recognised as a repeat, not applied twice.
    body, headers = _signed(payload)
    r = await api.post("/api/v1/courier/webhooks/shipment-status", content=body, headers=headers)
    assert r.json().get("duplicate") is True

    r = await api.get(f"/api/v1/orders/{order['order_id']}/tracking", headers=customer.headers)
    assert r.status_code == 200, r.text


async def test_webhook_signatures_are_enforced(api, world):
    payload = {"event_id": "evt_x", "shipment_id": "SBX1", "status": "delivered"}
    body, headers = _signed(payload, secret="not-our-secret")
    r = await api.post("/api/v1/courier/webhooks/shipment-status", content=body, headers=headers)
    assert r.status_code == 401

    body, headers = _signed(payload, t=int(time.time()) - 3600)
    r = await api.post("/api/v1/courier/webhooks/shipment-status", content=body, headers=headers)
    assert r.status_code == 401, "a replay outside the window is refused even with a valid signature"

    body, headers = _signed(payload)
    r = await api.post("/api/v1/courier/webhooks/shipment-status", content=body + b" ", headers=headers)
    assert r.status_code == 401, "a body altered after signing is refused"

    r, _ = await _push(api, f"SBX-UNKNOWN-{uuid.uuid4().hex[:6]}", "delivered")
    assert r.status_code == 200 and r.json()["matched"] is False


# ---------------------------------------------------------------------------
# After delivery: invoice, reorder, returns
# ---------------------------------------------------------------------------


async def _delivered(api, make_customer, make_product, place_order, make_admin, fake_courier):
    customer, order, sid, manager, product = await _dispatched(api, make_customer, make_product, place_order,
                                                               make_admin, fake_courier)
    r, _ = await _push(api, sid, "delivered", cod={"collected": True, "amount_lkr": order["total_amount"]})
    assert r.status_code == 200, r.text
    return customer, order, manager, product


async def test_invoice_and_reorder(api, world, make_customer, make_product, place_order, make_admin, fake_courier):
    customer, order, _, product = await _delivered(api, make_customer, make_product, place_order, make_admin, fake_courier)
    r = await api.get(f"/api/v1/orders/{order['order_id']}/invoice", headers=customer.headers)
    assert r.status_code == 200 and r.content.startswith(b"%PDF")

    r = await api.post(f"/api/v1/orders/{order['order_id']}/reorder", headers=customer.headers)
    assert r.status_code == 200, r.text
    r = await api.get("/api/v1/cart", headers=customer.headers)
    assert str(product) in r.text


async def test_a_returnless_refund_pays_back_to_the_wallet(
    api, world, make_customer, make_product, place_order, make_admin, fake_courier
):
    customer, order, manager, _ = await _delivered(api, make_customer, make_product, place_order, make_admin, fake_courier)
    oid = order["order_id"]

    r = await api.post(f"/api/v1/orders/{oid}/return/images", files={"file": ("rice.png", PNG, "image/png")},
                       headers=customer.headers)
    assert r.status_code == 200, r.text
    photo = r.json()["data"]["path"]

    r = await api.post(f"/api/v1/orders/{oid}/return", json={"reason": "Damaged", "images": []},
                       headers=customer.headers)
    assert r.status_code == 422, "a return from the app needs a photo"
    r = await api.post(f"/api/v1/orders/{oid}/return",
                       json={"reason": "Damaged", "comments": "Bag was torn", "images": [photo]},
                       headers=customer.headers)
    assert r.status_code == 200, r.text

    support = await make_admin("customer_support", branch_id=None, api=api)
    r = await api.post(f"/api/v1/admin/orders/{oid}/return/note", json={"note": "Customer sent a video too"},
                       headers=support.headers)
    assert r.status_code == 200, r.text
    r = await api.post(f"/api/v1/admin/orders/{oid}/return/approve", json={"resolution": "returnless_refund"},
                       headers=support.headers)
    assert r.status_code in (400, 403), "support annotates; the branch decides"

    r = await api.post(f"/api/v1/admin/orders/{oid}/return/approve",
                       json={"resolution": "returnless_refund", "note": "Photo shows damage"},
                       headers=manager.headers)
    assert r.status_code == 200, r.text

    r = await api.get("/api/v1/wallet", headers=customer.headers)
    balance = _balance(r)
    assert balance > 0, "the refund lands in the wallet"
    assert balance <= float(order["total_amount"]) + 0.01, "never more than was paid"


async def test_a_rejected_return_refunds_nothing(api, world, make_customer, make_product, place_order,
                                                 make_admin, fake_courier):
    customer, order, manager, _ = await _delivered(api, make_customer, make_product, place_order, make_admin, fake_courier)
    oid = order["order_id"]
    r = await api.post(f"/api/v1/orders/{oid}/return/images", files={"file": ("x.png", PNG, "image/png")},
                       headers=customer.headers)
    photo = r.json()["data"]["path"]
    r = await api.post(f"/api/v1/orders/{oid}/return", json={"reason": "Changed my mind", "images": [photo]},
                       headers=customer.headers)
    assert r.status_code == 200, r.text
    r = await api.post(f"/api/v1/admin/orders/{oid}/return/reject", json={"note": "Opened and used"},
                       headers=manager.headers)
    assert r.status_code == 200, r.text
    r = await api.get("/api/v1/wallet", headers=customer.headers)
    assert _balance(r) == 0.0


async def test_only_delivered_orders_can_be_returned(api, world, make_customer, make_product, place_order,
                                                     fake_courier):
    customer = await make_customer()
    order = await place_order(customer, await make_product(), 1)
    r = await api.post(f"/api/v1/orders/{order['order_id']}/return/images",
                       files={"file": ("x.png", PNG, "image/png")}, headers=customer.headers)
    assert r.status_code == 400, r.text
    other = await make_customer()
    r = await api.post(f"/api/v1/orders/{order['order_id']}/return",
                       json={"reason": "x", "images": ["/uploads/returns/x.png"]}, headers=other.headers)
    assert r.status_code in (400, 404), "nobody returns someone else's order"


# ---------------------------------------------------------------------------
# Paying from the wallet
# ---------------------------------------------------------------------------


async def test_paying_with_the_wallet_and_getting_it_back_on_cancel(
    api, db, world, make_customer, make_product, fake_courier, app_redis
):
    from decimal import Decimal

    from app.config.redis import RedisKeys
    from app.models.wallet import WalletTransactionType
    from app.services.wallet_service import WalletService

    customer = await make_customer()
    await WalletService.apply_transaction(db, user_id=customer.user_id, tx_type=WalletTransactionType.REFUND,
                                          amount=Decimal("300"), title="ZZ credit", order_id=None, order_number=None)
    await db.commit()
    product = await make_product(price="250.00")
    await app_redis.setex(RedisKeys.branch_context(str(customer.user_id)), 600,
                          json.dumps({"branch_id": str(world.branch_id), "branch_name": "ZZ"}))
    await api.post("/api/v1/cart/items", json={"product_id": str(product), "quantity": 2, "price": "250",
                                               "name": "ZZ"}, headers=customer.headers)
    r = await api.post("/api/v1/orders", json={
        "delivery_address_id": str(customer.address_id), "contact_phone": customer.phone,
        "alternate_phone": "+94719999999", "payment_method": "COD", "use_wallet": True, "wallet_amount": "200"},
        headers=customer.headers)
    assert r.status_code == 201, r.text
    order = r.json()["data"]
    assert order["wallet_deduction"] == pytest.approx(200.0), "a chosen wallet amount is honoured, not more"
    r = await api.get("/api/v1/wallet", headers=customer.headers)
    assert _balance(r) == pytest.approx(100.0)

    r = await api.post(f"/api/v1/orders/{order['order_id']}/cancel", headers=customer.headers)
    assert r.status_code == 200, r.text
    r = await api.get("/api/v1/wallet", headers=customer.headers)
    assert _balance(r) == pytest.approx(300.0), "cancelling puts the wallet back whole"


# ---------------------------------------------------------------------------
# Cart operations
# ---------------------------------------------------------------------------


async def test_cart_operations(api, world, make_customer, make_product, app_redis, fake_courier):
    from app.config.redis import RedisKeys

    c = await make_customer()
    await app_redis.setex(RedisKeys.branch_context(str(c.user_id)), 600,
                          json.dumps({"branch_id": str(world.branch_id), "branch_name": "ZZ"}))
    a = await make_product(price="100.00", stock=10)
    b = await make_product(price="50.00", stock=10)
    for p in (a, b):
        r = await api.post("/api/v1/cart/items", json={"product_id": str(p), "quantity": 1, "price": "1",
                                                       "name": "ZZ"}, headers=c.headers)
        assert r.status_code == 200, r.text

    r = await api.put(f"/api/v1/cart/items/{a}", json={"quantity": 3}, headers=c.headers)
    assert r.status_code == 200, r.text
    r = await api.put(f"/api/v1/cart/items/{a}", json={"quantity": 99}, headers=c.headers)
    assert r.status_code == 400, "no more than the branch has"
    r = await api.get("/api/v1/cart/count", headers=c.headers)
    assert r.status_code == 200 and "4" in r.text

    r = await api.post("/api/v1/cart/validate", headers=c.headers)
    assert r.status_code == 200, r.text

    r = await api.delete(f"/api/v1/cart/items/{b}", headers=c.headers)
    assert r.status_code == 200, r.text
    r = await api.get("/api/v1/cart", headers=c.headers)
    assert str(b) not in r.text and r.json()["data"]["totals"]["subtotal"] == pytest.approx(300.0)

    r = await api.delete("/api/v1/cart", headers=c.headers)
    assert r.status_code == 200
    r = await api.get("/api/v1/cart", headers=c.headers)
    assert r.json()["data"]["items"] == []
