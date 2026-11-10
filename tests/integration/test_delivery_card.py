"""Real Online DB + fake gateway; Express arbitration is tested in its repo."""
from decimal import Decimal
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from sqlalchemy import func, select

from app.config.settings import settings
from app.models.order import Order
from app.models.payment import PaymentAttempt
from app.services.courier_service import CourierService
from tests.integration.test_online_payments import _place, marx  # noqa: F401


@pytest.mark.asyncio
async def test_cod_at_door_captures_once_without_rebooking(
    api, db, app_redis, world, make_customer, make_product, fake_courier, marx, monkeypatch,  # noqa: F811
):
    monkeypatch.setattr(settings, "finance_outbox_enabled", True)
    customer = await make_customer()
    product = await make_product(price="500.00", stock=10)
    response = await _place(api, app_redis, world, customer, product, method="COD")
    assert response.status_code == 201, response.text
    original = response.json()["data"]
    order = await db.get(Order, UUID(original["order_id"]))
    order.status = "out_for_delivery"
    order.courier_shipment_id = "SXP-DOOR-TEST"
    await db.commit()
    monkeypatch.setattr(settings, "delivery_card_payment_enabled", True)

    async def courier(shipment_id, **kwargs):
        from app.services import courier_credentials

        assert courier_credentials.active_target().branch_id == world.branch_id
        assert shipment_id == "SXP-DOOR-TEST"
        assert Decimal(kwargs["amount"]) == Decimal(str(original["total_amount"]))
        return {"state": "pending" if kwargs["operation"] == "reserve" else kwargs["operation"]}

    mock = AsyncMock(side_effect=courier)
    monkeypatch.setattr(CourierService, "delivery_card_payment", mock)
    path = f"/api/v1/payments/orders/{order.order_id}/pay"
    first = await api.post(path, headers=customer.headers)
    assert first.status_code == 200, first.text
    action = first.json()["data"]["payment_action"]
    resumed = await api.post(path, headers=customer.headers)
    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["data"]["payment_action"] == action
    db.expire_all()
    payment = await db.scalar(select(PaymentAttempt).where(PaymentAttempt.purpose == "delivery_card"))
    return_url = f"{action['return_url']}?trId={payment.gateway_ref}&merchantRID={payment.reference}"
    for _ in range(2):
        paid = await api.post(f"/api/v1{action['complete_path']}", headers=customer.headers,
                              json={"return_url": return_url})
        assert paid.status_code == 200, paid.text
        assert paid.json()["data"]["outcome"] == "paid"
    db.expire_all()
    fresh = await db.get(Order, UUID(original["order_id"]))
    assert fresh.status == "out_for_delivery" and fresh.payment_status == "paid"
    assert fresh.payment_method == "MARX_OTHER" and fresh.delivery_card_state == "paid"
    assert fresh.courier_shipment_id == "SXP-DOOR-TEST"
    assert await db.scalar(select(func.count()).select_from(PaymentAttempt).where(
        PaymentAttempt.order_id == fresh.order_id, PaymentAttempt.purpose == "delivery_card")) == 1
    assert [call.kwargs["operation"] for call in mock.await_args_list] == ["reserve", "paid"]
    # The existing Finance feed sees the gateway receipt once, before Express
    # can complete delivery. It must not book rider cash for this payment.
    from app.models.finance_outbox import FinanceOutbox

    receipts = (await db.execute(select(FinanceOutbox).where(
        FinanceOutbox.aggregate_id == fresh.order_id,
        FinanceOutbox.event_type == "payment.received"))).scalars().all()
    assert len(receipts) == 1
    assert Decimal(receipts[0].payload["amount"]) == fresh.total_amount
    assert receipts[0].payload["method_code"] == "MARX_OTHER"
    fresh.status = "delivered"
    await db.commit()
    delivered = (await db.execute(select(FinanceOutbox).where(
        FinanceOutbox.aggregate_id == fresh.order_id,
        FinanceOutbox.event_type == "order.status"))).scalars().all()
    delivered = [event for event in delivered if event.payload.get("to_status") == "delivered"]
    assert len(delivered) == 1 and receipts[0].seq < delivered[0].seq


@pytest.fixture
async def door_checkout(
    api, db, app_redis, world, make_customer, make_product, fake_courier, marx, monkeypatch,  # noqa: F811
):
    """Exercise the customer API and Marx HTTP adapter using disposable data."""
    customer = await make_customer()
    product = await make_product(price="500.00", stock=10)
    response = await _place(api, app_redis, world, customer, product, method="COD")
    assert response.status_code == 201, response.text
    order = await db.get(Order, UUID(response.json()["data"]["order_id"]))
    order.status = "out_for_delivery"
    order.courier_shipment_id = "SXP-DOOR-SIMULATOR"
    await db.commit()
    monkeypatch.setattr(settings, "delivery_card_payment_enabled", True)
    monkeypatch.setattr(settings, "finance_outbox_enabled", True)
    shipment_id = order.courier_shipment_id
    amount = order.total_amount

    async def courier(requested_shipment_id, **payload):
        assert requested_shipment_id == shipment_id
        assert Decimal(payload["amount"]) == amount
        return {"state": "pending" if payload["operation"] == "reserve" else payload["operation"]}

    courier_mock = AsyncMock(side_effect=courier)
    monkeypatch.setattr(CourierService, "delivery_card_payment", courier_mock)
    response = await api.post(f"/api/v1/payments/orders/{order.order_id}/pay", headers=customer.headers)
    assert response.status_code == 200, response.text
    action = response.json()["data"]["payment_action"]
    attempt = await db.scalar(select(PaymentAttempt).where(PaymentAttempt.order_id == order.order_id))
    assert marx.orders[attempt.gateway_ref]["returnUrl"] == action["return_url"]
    return customer, order.order_id, attempt.attempt_id, action, courier_mock


@pytest.mark.asyncio
@pytest.mark.parametrize("gateway_outcome,expected_outcome", [("PENDING", "pending"), ("FAILURE", "failed")])
async def test_door_simulator_keeps_cod_until_gateway_answer(
    api, db, door_checkout, marx, gateway_outcome, expected_outcome,  # noqa: F811
):
    customer, order_id, attempt_id, action, courier = door_checkout
    attempt = await db.get(PaymentAttempt, attempt_id)
    marx.outcome = gateway_outcome
    response = await api.post(f"/api/v1{action['complete_path']}", headers=customer.headers,
        json={"return_url": f"{action['return_url']}?trId={attempt.gateway_ref}&merchantRID={attempt.reference}"})
    assert response.status_code == 200, response.text
    assert response.json()["data"]["outcome"] == expected_outcome
    db.expire_all()
    order = await db.get(Order, order_id)
    assert order.payment_method == "COD" and order.payment_status != "paid"
    assert order.status == "out_for_delivery"
    operations = [call.kwargs["operation"] for call in courier.await_args_list]
    assert operations == (["reserve"] if expected_outcome == "pending" else ["reserve", "failed"])
    shown = await api.get(f"/api/v1/orders/{order_id}", headers=customer.headers)
    assert shown.status_code == 200, shown.text
    expected_state = "delivery_card_pending" if expected_outcome == "pending" else "on_delivery"
    assert shown.json()["data"]["payment"]["state"] == expected_state


@pytest.mark.asyncio
async def test_door_simulator_recovers_after_gateway_outage(api, db, door_checkout, marx):  # noqa: F811
    from app.payments.delivery_card import sweep
    from app.payments.service import _now

    customer, order_id, attempt_id, action, courier = door_checkout
    attempt = await db.get(PaymentAttempt, attempt_id)
    marx.outcome = "PENDING"
    response = await api.post(f"/api/v1{action['complete_path']}", headers=customer.headers,
        json={"return_url": f"{action['return_url']}?trId={attempt.gateway_ref}&merchantRID={attempt.reference}"})
    assert response.status_code == 200, response.text
    assert response.json()["data"]["outcome"] == "pending"

    marx.outcome = "DOWN"
    await sweep(db, _now())
    db.expire_all()
    order = await db.get(Order, order_id)
    assert order.delivery_card_state == "pending" and order.payment_status != "paid"
    assert [call.kwargs["operation"] for call in courier.await_args_list] == ["reserve"]

    marx.outcome = "SUCCESS"
    await sweep(db, _now())
    await sweep(db, _now())
    db.expire_all()
    order = await db.get(Order, order_id)
    assert order.delivery_card_state == "paid" and order.payment_status == "paid"
    assert order.status == "out_for_delivery"
    assert len(marx.orders) == 1
    assert [call.kwargs["operation"] for call in courier.await_args_list] == ["reserve", "paid"]


@pytest.mark.asyncio
async def test_door_simulator_retries_lost_rider_acknowledgement(api, db, door_checkout, marx):  # noqa: F811
    from app.models.finance_outbox import FinanceOutbox
    from app.payments.delivery_card import sweep
    from app.payments.service import _now
    from app.services.courier_service import CourierUnavailableError

    customer, order_id, attempt_id, action, courier = door_checkout
    courier.side_effect = CourierUnavailableError("simulated lost acknowledgement")
    attempt = await db.get(PaymentAttempt, attempt_id)
    response = await api.post(f"/api/v1{action['complete_path']}", headers=customer.headers,
        json={"return_url": f"{action['return_url']}?trId={attempt.gateway_ref}&merchantRID={attempt.reference}"})
    assert response.status_code == 200, response.text
    assert response.json()["data"]["outcome"] == "paid"
    db.expire_all()
    order = await db.get(Order, order_id)
    assert order.payment_status == "paid" and order.delivery_card_state == "syncing"

    courier.side_effect = None
    courier.return_value = {"state": "paid"}
    await sweep(db, _now())
    await sweep(db, _now())
    db.expire_all()
    order = await db.get(Order, order_id)
    assert order.delivery_card_state == "paid"
    assert [call.kwargs["operation"] for call in courier.await_args_list] == ["reserve", "paid", "paid"]
    assert len(marx.captures) == 1
    receipts = await db.scalar(select(func.count()).select_from(FinanceOutbox).where(
        FinanceOutbox.aggregate_id == order_id, FinanceOutbox.event_type == "payment.received"))
    assert receipts == 1


@pytest.mark.asyncio
async def test_door_simulator_rejects_forged_return(api, db, door_checkout, marx):  # noqa: F811
    customer, order_id, _, action, courier = door_checkout
    response = await api.post(f"/api/v1{action['complete_path']}", headers=customer.headers,
        json={"return_url": f"{action['return_url']}?trId=WRONG&merchantRID=WRONG"})
    assert response.status_code == 400, response.text
    assert marx.captures == []
    db.expire_all()
    order = await db.get(Order, order_id)
    assert order.delivery_card_state == "pending" and order.payment_status != "paid"
    assert [call.kwargs["operation"] for call in courier.await_args_list] == ["reserve"]


@pytest.mark.asyncio
@pytest.mark.parametrize("code,option,icon", [("MARX_BANK", "PAY_BY_BANK_ACCOUNT", "bank"), ("FUTURE_QR", "TEST_QR", "qr")])
async def test_delivery_methods_follow_enabled_catalog_and_resume_one_checkout(
    api, db, app_redis, world, make_customer, make_product, fake_courier, marx, monkeypatch, code, option, icon,  # noqa: F811
):
    from app.payments import catalog

    method = catalog.MethodSpec(code=code, kind="online", icon=icon,
        name=f"Hosted {icon} payment", description="Provider payment page", legacy_type="GATEWAY",
        gateway="marx", option=option)
    monkeypatch.setattr(catalog, "METHODS", tuple(m for m in catalog.METHODS if m.code != code) + (method,))
    monkeypatch.setitem(catalog._BY_CODE, code, method)
    monkeypatch.setattr(settings, "delivery_card_payment_enabled", True)
    customer = await make_customer()
    other_customer = await make_customer()
    response = await _place(api, app_redis, world, customer, await make_product(), method="COD")
    assert response.status_code == 201, response.text
    order_id = UUID(response.json()["data"]["order_id"])
    order = await db.get(Order, order_id)
    order.status = "out_for_delivery"
    order.courier_shipment_id = "SXP-METHODS"
    await db.commit()
    courier = AsyncMock(return_value={"state": "pending"})
    monkeypatch.setattr(CourierService, "delivery_card_payment", courier)
    path = f"/api/v1/payments/orders/{order_id}"
    response = await api.get(path + "/methods", headers=customer.headers)
    assert response.status_code == 200, response.text
    methods = response.json()["data"]["methods"]
    assert code in [m["code"] for m in methods]
    assert "MARX_AMEX" not in [m["code"] for m in methods]
    response = await api.get(path + "/methods", headers=other_customer.headers)
    assert response.status_code == 404
    refused = await api.post(path + "/pay", headers=customer.headers, json={"method": "MARX_AMEX"})
    assert refused.status_code == 400
    assert not marx.orders and courier.await_count == 0

    response = await api.post(path + "/pay", headers=customer.headers, json={"method": code})
    assert response.status_code == 200, response.text
    action = response.json()["data"]["payment_action"]
    assert list(marx.orders.values())[0]["paymentMethod"] == option
    response = await api.get(path + "/methods", headers=customer.headers)
    assert [m["code"] for m in response.json()["data"]["methods"]] == [code]
    refused = await api.post(path + "/pay", headers=customer.headers, json={"method": "MARX_OTHER"})
    assert refused.status_code == 409
    response = await api.post(path + "/pay", headers=customer.headers, json={"method": code})
    assert response.json()["data"]["payment_action"] == action
    assert len(marx.orders) == 1
    db.expire_all()
    attempt = await db.scalar(select(PaymentAttempt).where(PaymentAttempt.order_id == order_id))
    assert attempt.method_code == code
    courier.return_value = {"state": "paid"}
    response = await api.post(path + "/complete", headers=customer.headers,
        json={"return_url": f"{action['return_url']}?trId={attempt.gateway_ref}&merchantRID={attempt.reference}"})
    assert response.status_code == 200, response.text
    assert response.json()["data"]["outcome"] == "paid"
    assert response.json()["data"]["payment_method"] == code
    assert [call.kwargs["operation"] for call in courier.await_args_list] == ["reserve", "paid"]
