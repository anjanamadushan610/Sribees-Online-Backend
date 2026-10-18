"""
Admin order desk: the list, the fulfilment state machine, dispatch to the
courier, exports, invoices and escalations -- over HTTP, per role.

The flow under test is the branch floor's whole day: an order arrives, is
confirmed, packed, handed to SribeesExpress (the fake one, at the network), and
the paperwork comes out right.
"""
from __future__ import annotations

import pytest


async def _order(make_customer, make_product, place_order, qty=2):
    customer = await make_customer()
    product = await make_product(price="300.00", stock=50)
    order = await place_order(customer, product, qty)
    return customer, product, order


async def _move(api, admin, order_id, status):
    return await api.patch(
        f"/api/v1/admin/orders/{order_id}/status", json={"status": status}, headers=admin.headers
    )


async def test_the_order_list_filters_and_counts(api, world, make_customer, make_product, place_order, make_admin):
    _, _, order = await _order(make_customer, make_product, place_order)
    admin = await make_admin("super_admin", api=api)

    r = await api.get("/api/v1/admin/orders", headers=admin.headers)
    assert r.status_code == 200, r.text
    assert order["order_id"] in r.text

    for params in [
        {"search": order["order_number"]},
        {"order_status": "pending"},
        {"order_statuses": "pending,confirmed"},
        {"branch_id": str(world.branch_id)},
        {"from_date": "2020-01-01", "to_date": "2099-12-31"},
    ]:
        r = await api.get("/api/v1/admin/orders", params=params, headers=admin.headers)
        assert r.status_code == 200, (params, r.text)
        assert order["order_id"] in r.text, params

    r = await api.get("/api/v1/admin/orders", params={"order_status": "delivered", "search": order["order_number"]},
                      headers=admin.headers)
    assert order["order_id"] not in r.text


async def test_order_detail_and_invoice(api, world, make_customer, make_product, place_order, make_admin):
    _, _, order = await _order(make_customer, make_product, place_order)
    admin = await make_admin("super_admin", api=api)
    r = await api.get(f"/api/v1/admin/orders/{order['order_id']}", headers=admin.headers)
    assert r.status_code == 200, r.text
    assert r.json()["data"]["order_number"] == order["order_number"]

    r = await api.get(f"/api/v1/admin/orders/{order['order_id']}/invoice", headers=admin.headers)
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("application/pdf")
    assert r.content.startswith(b"%PDF")


async def test_the_fulfilment_flow_to_the_courier(
    api, world, make_customer, make_product, place_order, make_admin, fake_courier
):
    _, _, order = await _order(make_customer, make_product, place_order)
    oid = order["order_id"]
    manager = await make_admin("branch_manager", api=api)

    r = await api.get(f"/api/v1/admin/orders/{oid}/next-statuses", headers=manager.headers)
    assert r.status_code == 200, r.text
    assert "confirmed" in [a["status"] for a in r.json()["data"]["actions"]]

    # A jump the state machine does not allow is refused, and nothing moves.
    r = await _move(api, manager, oid, "delivered")
    assert r.status_code == 400, r.text

    for step in ["confirmed", "packing", "packed"]:
        r = await _move(api, manager, oid, step)
        assert r.status_code == 200, (step, r.text)
        assert r.json()["data"]["status"] == step

    r = await api.post(f"/api/v1/admin/orders/{oid}/courier/request-pickup", headers=manager.headers)
    assert r.status_code == 200, r.text
    booked = [s for s in fake_courier.shipments.values() if s["merchant_order_ref"]]
    assert booked, "the parcel must be booked with the courier"
    assert booked[-1]["payment_method"] == "cod"
    assert float(booked[-1]["cod_amount"]) == pytest.approx(float(order["total_amount"]))

    r = await api.get(f"/api/v1/admin/orders/{oid}", headers=manager.headers)
    detail = r.json()["data"]
    assert detail["status"] == "ready_for_pickup"

    # Pressing Request Pickup again is a double click, not a second parcel.
    before = len(fake_courier.shipments)
    r = await api.post(f"/api/v1/admin/orders/{oid}/courier/request-pickup", headers=manager.headers)
    assert r.status_code == 200, r.text
    assert len(fake_courier.shipments) == before

    r = await api.post(f"/api/v1/admin/orders/{oid}/courier/refresh", headers=manager.headers)
    assert r.status_code == 200, r.text
    r = await api.get(f"/api/v1/admin/orders/{oid}/courier/label", headers=manager.headers)
    assert r.status_code == 200, r.text
    label = r.json()["data"]
    assert label["order_number"] == order["order_number"] and label["waybill_id"]


async def test_support_can_read_and_escalate_but_not_dispatch(
    api, world, make_customer, make_product, place_order, make_admin
):
    _, _, order = await _order(make_customer, make_product, place_order)
    oid = order["order_id"]
    support = await make_admin("customer_support", branch_id=None, api=api)

    r = await api.get(f"/api/v1/admin/orders/{oid}", headers=support.headers)
    assert r.status_code == 200, r.text
    r = await api.get(f"/api/v1/admin/orders/{oid}/next-statuses", headers=support.headers)
    assert r.status_code == 200, r.text
    assert r.json()["data"]["actions"] == [] and r.json()["data"]["can_override"] is False

    r = await _move(api, support, oid, "confirmed")
    assert r.status_code == 400, r.text
    r = await api.post(f"/api/v1/admin/orders/{oid}/courier/request-pickup", headers=support.headers)
    assert r.status_code == 403, r.text

    # Their route to action is an escalation to the branch.
    r = await api.post(
        f"/api/v1/admin/orders/{oid}/escalations",
        json={"category": "other", "message": "Customer asked to deliver after 5pm"},
        headers=support.headers,
    )
    assert r.status_code == 200, r.text
    esc_id = r.json()["data"]["escalation_id"]

    r = await api.patch(
        f"/api/v1/admin/orders/{oid}/escalations/{esc_id}",
        json={"status": "resolved", "resolution_note": "noted"},
        headers=support.headers,
    )
    assert r.status_code == 403, "support raises escalations; the branch resolves them"

    # Check that Support's actionable queue excludes unresolved outbox tickets they created
    r = await api.get("/api/v1/admin/orders/escalations/queue", params={"for_role": "customer_support"}, headers=support.headers)
    assert r.status_code == 200, r.text
    item_ids = [item["escalation_id"] for item in r.json()["data"]["items"]]
    assert esc_id not in item_ids, "Support outbox tickets without BM response must not appear in support queue"

    manager = await make_admin("branch_manager", api=api)
    r = await api.get(f"/api/v1/admin/orders/{oid}/escalations", headers=manager.headers)
    assert r.status_code == 200 and esc_id in r.text
    for status_ in ["acknowledged", "resolved"]:
        r = await api.patch(
            f"/api/v1/admin/orders/{oid}/escalations/{esc_id}",
            json={"status": status_, "resolution_note": "Rider told"},
            headers=manager.headers,
        )
        assert r.status_code == 200, r.text
        assert r.json()["data"]["status"] == status_

    # Now that the BM has responded/resolved, it is visible in the support inbox
    r = await api.get("/api/v1/admin/orders/escalations/queue", params={"for_role": "customer_support", "status": "all"}, headers=support.headers)
    assert r.status_code == 200, r.text
    item_ids = [item["escalation_id"] for item in r.json()["data"]["items"]]
    assert esc_id in item_ids


async def test_exports(api, world, make_customer, make_product, place_order, make_admin):
    _, _, order = await _order(make_customer, make_product, place_order)
    admin = await make_admin("super_admin", api=api)

    r = await api.get("/api/v1/admin/orders/export/csv", params={"order_ids": order["order_id"]},
                      headers=admin.headers)
    assert r.status_code == 200, r.text
    assert "text/csv" in r.headers["content-type"]
    assert order["order_number"] in r.text

    r = await api.get("/api/v1/admin/orders/export/csv", params={"search": order["order_number"]},
                      headers=admin.headers)
    assert r.status_code == 200 and order["order_number"] in r.text

    r = await api.get("/api/v1/admin/orders/export/pdf", params={"search": order["order_number"]},
                      headers=admin.headers)
    assert r.status_code == 200, r.text
    assert r.content.startswith(b"%PDF")


async def test_a_prepaid_order_is_not_dispatched_before_payment(
    api, world, make_customer, make_product, make_admin, fake_courier, db
):
    """A parcel collected for an unpaid card order is goods given away."""
    from sqlalchemy import update

    from app.models.order import Order

    customer = await make_customer()
    product = await make_product()
    from tests.integration.conftest import Customer  # noqa: F401  (type only)

    manager = await make_admin("branch_manager", api=api)
    # Build a packed, unpaid gateway order directly: the gateway itself is an
    # external party and is exercised in test_marx_ipg_service.
    import uuid as _uuid
    from decimal import Decimal

    from app.models.order import OrderItem

    oid = _uuid.uuid4()
    db.add(
        Order(
            order_id=oid,
            order_number=f"ZZ-{_uuid.uuid4().hex[:8]}",
            user_id=customer.user_id,
            branch_id=world.branch_id,
            status="packed",
            payment_status="pending",
            payment_method="MARX_OTHER",
            subtotal=Decimal("250"),
            total_amount=Decimal("600"),
            shipping_amount=Decimal("350"),
            delivery_address_id=customer.address_id,
            contact_phone=customer.phone,
        )
    )
    await db.flush()
    db.add(OrderItem(order_id=oid, product_id=product, product_name="x", quantity=1,
                     unit_price=Decimal("250"), subtotal=Decimal("250")))
    await db.commit()

    before = len(fake_courier.shipments)
    r = await api.post(f"/api/v1/admin/orders/{oid}/courier/request-pickup", headers=manager.headers)
    assert r.status_code == 409, r.text
    assert "paid" in r.text.lower()
    assert len(fake_courier.shipments) == before

    await db.execute(update(Order).where(Order.order_id == oid).values(payment_status="paid"))
    await db.commit()
    r = await api.post(f"/api/v1/admin/orders/{oid}/courier/request-pickup", headers=manager.headers)
    assert r.status_code == 200, r.text
