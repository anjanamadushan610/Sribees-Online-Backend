"""Real Online DB + fake gateway; Express arbitration is tested in its repo."""
from decimal import Decimal
from unittest.mock import AsyncMock

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
    from uuid import UUID

    order = await db.get(Order, UUID(original["order_id"]))
    order.status = "out_for_delivery"
    order.courier_shipment_id = "SXP-DOOR-TEST"
    await db.commit()
    monkeypatch.setattr(settings, "delivery_card_payment_enabled", True)

    async def courier(shipment_id, **kwargs):
        from app.services import courier_credentials

        assert courier_credentials.current().branch_id == world.branch_id
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
