"""
A return a rider collects: approved with a reverse pickup, the courier books
the reverse leg and hands us a code, and the refund is paid only when the goods
are back at the branch -- once, however many times the courier says so.
"""
from __future__ import annotations

import pytest

from tests.integration.test_order_lifecycle import (  # noqa: F401 - _webhook_secret is an autouse fixture
    PNG,
    _balance,
    _delivered,
    _push,
    _webhook_secret,
)


@pytest.fixture(autouse=True)
def _return_fee_on(monkeypatch):
    from app.config.settings import settings

    monkeypatch.setattr(settings, "return_fee_charged_to_customer", True)


async def _return_requested(api, make_customer, make_product, place_order, make_admin, fake_courier):
    customer, order, manager, _ = await _delivered(api, make_customer, make_product, place_order, make_admin,
                                                   fake_courier)
    oid = order["order_id"]
    r = await api.post(f"/api/v1/orders/{oid}/return/images", files={"file": ("x.png", PNG, "image/png")},
                       headers=customer.headers)
    photo = r.json()["data"]["path"]
    r = await api.post(f"/api/v1/orders/{oid}/return", json={"reason": "Wrong item", "images": [photo]},
                       headers=customer.headers)
    assert r.status_code == 200, r.text
    return customer, order, manager


async def test_a_reverse_pickup_refunds_when_the_goods_are_back(
    api, world, make_customer, make_product, place_order, make_admin, fake_courier
):
    customer, order, manager = await _return_requested(api, make_customer, make_product, place_order,
                                                       make_admin, fake_courier)
    oid = order["order_id"]
    r = await api.post(f"/api/v1/admin/orders/{oid}/return/approve", json={"resolution": "reverse_pickup"},
                       headers=manager.headers)
    assert r.status_code == 200, r.text
    assert len(fake_courier.returns) == 1, "the reverse leg is booked with the courier"
    rid = next(iter(fake_courier.returns))
    assert _balance(await api.get("/api/v1/wallet", headers=customer.headers)) == 0.0, (
        "no refund while the goods are still with the customer"
    )

    r = await api.post(f"/api/v1/admin/orders/{oid}/return/regenerate-code", headers=manager.headers)
    assert r.status_code == 200, r.text

    shipment_id = next(sid for sid, s in fake_courier.shipments.items()
                       if order["order_number"] in s["merchant_order_ref"])
    for status in ("picked_up", "in_transit"):
        r, _ = await _push(api, shipment_id, status, return_id=rid)
        assert r.status_code == 200, r.text
    assert _balance(await api.get("/api/v1/wallet", headers=customer.headers)) == 0.0

    r, payload = await _push(api, shipment_id, "delivered", return_id=rid)
    assert r.status_code == 200 and r.json()["matched"] is True, r.text
    refunded = _balance(await api.get("/api/v1/wallet", headers=customer.headers))
    # 2 x Rs 400 back, less the courier's Rs 250 return fee -- its own line.
    assert refunded == pytest.approx(550.0)
    lines = await api.get("/api/v1/wallet/transactions", headers=customer.headers)
    body = lines.json().get("data", lines.json())
    titles = {(t["title"], float(t["amount"])) for t in (body.get("transactions") or body.get("items") or [])}
    assert {("Return refund", 800.0), ("Return delivery charge", 250.0)} <= titles, titles

    # The courier says "delivered" again (a new event id): still one refund.
    r, _ = await _push(api, shipment_id, "delivered", return_id=rid)
    assert r.status_code == 200
    assert _balance(await api.get("/api/v1/wallet", headers=customer.headers)) == pytest.approx(refunded)


async def test_a_retried_return_booking_is_not_a_second_leg(
    api, world, make_customer, make_product, place_order, make_admin, fake_courier
):
    customer, order, manager = await _return_requested(api, make_customer, make_product, place_order,
                                                       make_admin, fake_courier)
    oid = order["order_id"]
    r = await api.post(f"/api/v1/admin/orders/{oid}/return/approve", json={"resolution": "reverse_pickup"},
                       headers=manager.headers)
    assert r.status_code == 200, r.text
    r = await api.post(f"/api/v1/admin/orders/{oid}/return/retry-booking", headers=manager.headers)
    assert r.status_code == 200, r.text
    assert len(fake_courier.returns) == 1, "an already-booked leg is not booked twice"


async def test_the_return_fee_never_takes_more_than_the_refund(
    api, world, make_customer, make_product, place_order, make_admin, fake_courier
):
    customer, order, manager, _ = await _delivered_cheap(api, make_customer, make_product, place_order,
                                                         make_admin, fake_courier)
    oid = order["order_id"]
    r = await api.post(f"/api/v1/admin/orders/{oid}/return/approve", json={"resolution": "reverse_pickup"},
                       headers=manager.headers)
    assert r.status_code == 200, r.text
    rid = next(rid for rid, leg in fake_courier.returns.items()
               if leg["merchant_order_ref"] == order["order_number"])
    shipment_id = next(sid for sid, s in fake_courier.shipments.items()
                       if order["order_number"] in s["merchant_order_ref"])
    r, _ = await _push(api, shipment_id, "delivered", return_id=rid)
    assert r.status_code == 200, r.text
    # Rs 100 back, Rs 250 fee: the fee stops at the refund, no debt.
    assert _balance(await api.get("/api/v1/wallet", headers=customer.headers)) == 0.0


async def _delivered_cheap(api, make_customer, make_product, place_order, make_admin, fake_courier):
    from tests.integration.test_order_lifecycle import _push as push

    customer = await make_customer()
    product = await make_product(price="100.00", stock=10)
    order = await place_order(customer, product, 1)
    manager = await make_admin("branch_manager", api=api)
    for step in ["confirmed", "packing", "packed"]:
        r = await api.patch(f"/api/v1/admin/orders/{order['order_id']}/status", json={"status": step},
                            headers=manager.headers)
        assert r.status_code == 200, r.text
    r = await api.post(f"/api/v1/admin/orders/{order['order_id']}/courier/request-pickup", headers=manager.headers)
    assert r.status_code == 200, r.text
    sid = next(s for s, v in fake_courier.shipments.items() if order["order_number"] in v["merchant_order_ref"])
    r, _ = await push(api, sid, "delivered", cod={"collected": True, "amount_lkr": order["total_amount"]})
    assert r.status_code == 200, r.text
    oid = order["order_id"]
    r = await api.post(f"/api/v1/orders/{oid}/return/images", files={"file": ("x.png", PNG, "image/png")},
                       headers=customer.headers)
    r = await api.post(f"/api/v1/orders/{oid}/return", json={"reason": "Wrong item",
                                                             "images": [r.json()["data"]["path"]]},
                       headers=customer.headers)
    assert r.status_code == 200, r.text
    return customer, order, manager, product


async def test_the_return_screen_names_the_fee_first(
    api, world, make_customer, make_product, place_order, make_admin, fake_courier
):
    customer, order, _, _ = await _delivered(api, make_customer, make_product, place_order, make_admin,
                                             fake_courier)
    r = await api.get(f"/api/v1/orders/{order['order_id']}/return/quote", headers=customer.headers)
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["return_fee"] == "250.00" and "250.00" in data["note"]
    si = await api.get(f"/api/v1/orders/{order['order_id']}/return/quote",
                       headers={**customer.headers, "Accept-Language": "si"})
    assert "රු. 250.00" in si.json()["data"]["note"]
    other = await make_customer()
    assert (await api.get(f"/api/v1/orders/{order['order_id']}/return/quote",
                          headers=other.headers)).status_code == 404


async def test_no_fee_is_taken_until_the_switch_is_on(
    api, world, make_customer, make_product, place_order, make_admin, fake_courier, monkeypatch
):
    from app.config.settings import settings

    monkeypatch.setattr(settings, "return_fee_charged_to_customer", False)
    customer, order, manager = await _return_requested(api, make_customer, make_product, place_order,
                                                       make_admin, fake_courier)
    oid = order["order_id"]
    r = await api.post(f"/api/v1/admin/orders/{oid}/return/approve", json={"resolution": "reverse_pickup"},
                       headers=manager.headers)
    assert r.status_code == 200, r.text
    rid = next(rid for rid, leg in fake_courier.returns.items()
               if leg["merchant_order_ref"] == order["order_number"])
    shipment_id = next(sid for sid, s in fake_courier.shipments.items()
                       if order["order_number"] in s["merchant_order_ref"])
    r, _ = await _push(api, shipment_id, "delivered", return_id=rid)
    assert r.status_code == 200, r.text
    assert _balance(await api.get("/api/v1/wallet", headers=customer.headers)) == pytest.approx(800.0)
