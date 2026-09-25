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
    assert 0 < refunded <= float(order["total_amount"]) + 0.01

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
