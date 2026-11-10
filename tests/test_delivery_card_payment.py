from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest

from app.config.settings import settings
from app.payments import delivery_card as door
from app.payments.gateways.base import GatewayAnswer, Outcome
from app.services.courier_service import CourierUnavailableError


@pytest.fixture(autouse=True)
def courier_scope(monkeypatch):
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def scoped(db, branch_id):
        yield

    monkeypatch.setattr(door.courier_credentials, "for_branch", scoped)


def order(**kwargs):
    return SimpleNamespace(**{
        "delivery_card_state": None, "delivery_card_reference": None,
        "branch_id": uuid4(), "order_id": uuid4(), "order_number": "TEST-1", "payment_method": "COD",
        "payment_status": "pending", "status": "out_for_delivery", "total_amount": Decimal("1500"),
        "courier_shipment_id": "SXP1", "courier_cod_collected": False, **kwargs,
    })


def attempt(**kwargs):
    return SimpleNamespace(**{
        "status": "pending", "reference": "DOOR-123", "amount": Decimal("1500"),
        "gateway_ref": "TR1", "method_code": "MARX_OTHER", "courier_synced": None, **kwargs,
    })


@pytest.mark.parametrize("lang", ["en", "si", "ta"])
def test_feature_off_then_eligible_and_pending_copy(monkeypatch, lang):
    monkeypatch.setattr(settings, "delivery_card_payment_enabled", False)
    assert door.payment_view(order(), lang) is None
    monkeypatch.setattr(settings, "delivery_card_payment_enabled", True)
    monkeypatch.setattr(settings, "marx_gateway_methods_enabled", True)
    assert door.payment_view(order(), lang)["can_pay"]
    assert not door.payment_view(order(courier_cod_collected=True), lang)
    assert not door.payment_view(order(status="delivered"), lang)
    assert not door.payment_view(order(total_amount=Decimal(0)), lang)
    for state in ("preparing", "syncing", "review"):
        view = door.payment_view(order(delivery_card_state=state), lang)
        assert not view["can_pay"] and view["message"] and view["state"] == "delivery_card_pending"


@pytest.mark.asyncio
async def test_paid_updates_money_before_express_and_does_not_rebook(monkeypatch):
    row = order(delivery_card_state="pending", delivery_card_reference="DOOR-123")
    pay = attempt()
    db = AsyncMock()

    async def courier(*args, **kwargs):
        db.commit.assert_awaited_once()
        assert row.payment_status == "paid" and pay.status == "paid"
        assert kwargs["amount"] == "1500" and kwargs["gateway_reference"] == "TR1"
        assert kwargs["operation"] == "paid"
        return {"state": "paid"}

    monkeypatch.setattr(door.CourierService, "delivery_card_payment", courier)
    assert await door.apply_answer(db, row, pay, GatewayAnswer(Outcome.PAID)) == Outcome.PAID
    assert row.status == "out_for_delivery"
    assert row.payment_method == "MARX_OTHER" and row.delivery_card_state == "paid"
    assert pay.courier_synced == "yes"


@pytest.mark.asyncio
async def test_express_outage_is_durable_and_idempotently_retried(monkeypatch):
    row = order(delivery_card_state="pending", delivery_card_reference="DOOR-123")
    pay = attempt()
    db = AsyncMock()
    courier = AsyncMock(side_effect=CourierUnavailableError("offline"))
    monkeypatch.setattr(door.CourierService, "delivery_card_payment", courier)
    await door.apply_answer(db, row, pay, GatewayAnswer(Outcome.PAID))
    assert pay.status == "paid" and pay.courier_synced is None
    assert row.delivery_card_state == "syncing"
    courier.side_effect = None
    courier.return_value = {"state": "paid"}
    await door.apply_answer(db, row, pay, GatewayAnswer(Outcome.PAID))
    assert row.delivery_card_state == "paid" and pay.courier_synced == "yes"
    assert courier.await_count == 2


@pytest.mark.asyncio
async def test_pending_never_releases_cash_and_failure_keeps_cod(monkeypatch):
    row = order(delivery_card_state="pending", delivery_card_reference="DOOR-123")
    pay = attempt()
    db = AsyncMock()
    courier = AsyncMock(return_value={"state": "failed"})
    monkeypatch.setattr(door.CourierService, "delivery_card_payment", courier)
    await door.apply_answer(db, row, pay, GatewayAnswer(Outcome.PENDING))
    courier.assert_not_awaited()
    await door.apply_answer(db, row, pay, GatewayAnswer(Outcome.FAILED))
    assert row.payment_method == "COD" and row.payment_status == "pending"
    assert row.delivery_card_state == "failed"


@pytest.mark.asyncio
async def test_crashed_start_recovery_rechecks_gateway_reference_under_lock(monkeypatch):
    courier = AsyncMock()
    monkeypatch.setattr(door.CourierService, "delivery_card_payment", courier)
    pay = attempt(status="started")
    row = order(delivery_card_state="pending")
    result = await door.apply_answer(AsyncMock(), row, pay,
                                    GatewayAnswer(Outcome.FAILED, code="UNOPENED_START"))
    assert result == Outcome.PENDING
    courier.assert_not_awaited()
    assert pay.status == "started"


@pytest.mark.asyncio
async def test_same_checkout_resumed_without_another_gateway_request(monkeypatch):
    monkeypatch.setattr(settings, "delivery_card_payment_enabled", True)
    monkeypatch.setattr(settings, "marx_gateway_methods_enabled", True)
    row = order(delivery_card_reference="DOOR-123", delivery_card_state="pending")
    monkeypatch.setattr(door, "lock_order", AsyncMock(return_value=row))
    db = AsyncMock()
    db.scalar.return_value = SimpleNamespace(status="pending", checkout_url="https://pay.test/1",
        gateway="marx", order_id=row.order_id)
    gateway = Mock(return_url="https://return.test")
    monkeypatch.setattr(door, "get_gateway", Mock(return_value=gateway))
    action = await door.start(db, row, SimpleNamespace(email="test@example.com"), lang="en")
    assert action["url"] == "https://pay.test/1"
    db.add.assert_not_called()


@pytest.mark.asyncio
async def test_late_payment_after_definitive_failure_is_refunded_once(monkeypatch):
    from app.payments import service

    refund = AsyncMock()
    monkeypatch.setattr(service, "_give_back", refund)
    courier = AsyncMock()
    monkeypatch.setattr(door.CourierService, "delivery_card_payment", courier)
    row = order(delivery_card_state="failed", delivery_card_reference="DOOR-123")
    pay = attempt(status="failed", courier_synced="yes")
    db = AsyncMock()
    await door.apply_answer(db, row, pay, GatewayAnswer(Outcome.PAID))
    await door.apply_answer(db, row, pay, GatewayAnswer(Outcome.PAID))
    refund.assert_awaited_once_with(db, row, pay, second=True)
    assert pay.raw["delivery_refunded_to_wallet"] is True
    assert row.payment_method == "COD" and row.delivery_card_state == "failed"
    courier.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancellation_rechecks_payment_under_lock(monkeypatch):
    from app.services.order_service import OrderService

    stale = order(delivery_card_state=None)
    current = order(delivery_card_state="pending", order_id=stale.order_id)
    lock = AsyncMock(return_value=current)
    monkeypatch.setattr(door, "lock_order", lock)
    refund = AsyncMock()
    monkeypatch.setattr(OrderService, "_apply_cancellation_effects", refund)
    with pytest.raises(ValueError, match="Card payment"):
        await OrderService.cancel_order(AsyncMock(), stale)
    lock.assert_awaited_once()
    refund.assert_not_awaited()
