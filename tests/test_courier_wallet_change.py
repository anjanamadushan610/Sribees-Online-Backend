"""Cash change credits use the real wallet and ledger transaction boundaries."""

from decimal import Decimal
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from app.config.settings import settings
from app.models.order import Order
from app.models.wallet import CourierWalletCredit, Wallet, WalletTransaction
from app.services import courier_wallet_change as change
from app.services.courier_credentials import CourierTarget
from tests.conftest import requires_db
from tests.test_linked_partner_wallet import money_db  # noqa: F401


def pending(**overrides):
    return {
        "request_id": 1,
        "client_id": 1,
        "environment": "live",
        "shipment_id": "TEST-SXP",
        "merchant_order_ref": "TEST-ORDER",
        "amount": "150.00",
        "order_amount": "850.00",
        "cash_received": "1000.00",
        **overrides,
    }


@pytest.mark.parametrize("value", ["-1", "0", "NaN", "Infinity", "0.001"])
def test_invalid_credit_amount_rejected(value):
    with pytest.raises(ValidationError):
        change.PendingCredit.model_validate(pending(amount=value))


def test_source_is_stable_for_key_rotation_and_scopes_accounts():
    a = CourierTarget("https://courier.example/api/v1/ecommerce", "key-one", "live")
    b = CourierTarget(a.base_url + "/", "key-two", "live")
    payload = change.PendingCredit.model_validate(pending())
    assert change.source_reference(a, payload) == change.source_reference(b, payload)
    assert change.source_reference(a, payload) != change.source_reference(
        a, change.PendingCredit.model_validate(pending(client_id=2))
    )


@requires_db
@pytest.mark.asyncio
async def test_credit_and_retry_preserve_wallet_ledger_and_ownership(
    money_db,  # noqa: F811 - imported shared pytest fixture
    monkeypatch,
):
    db, user = money_db
    target = CourierTarget(
        "https://courier.example/api/v1/ecommerce", "test-key", "live"
    )
    monkeypatch.setattr(settings, "finance_outbox_enabled", True)
    monkeypatch.setattr(
        change.courier_credentials, "resolve", AsyncMock(return_value=target)
    )
    notify = AsyncMock()
    monkeypatch.setattr(change.NotificationService, "notify", notify)
    order = Order(
        user_id=user.user_id,
        order_number=f"TEST-{uuid4().hex[:8]}",
        subtotal=Decimal("850"),
        total_amount=Decimal("850"),
        payment_method="COD",
        courier_shipment_id=f"TEST-{uuid4().hex[:8]}",
    )
    db.add(order)
    await db.flush()
    data = pending(
        shipment_id=order.courier_shipment_id, merchant_order_ref=order.order_number
    )
    tx = await change.credit_request(db, target, data)
    assert await change.credit_request(db, target, data) == tx
    assert (
        await db.scalar(select(Wallet.balance).where(Wallet.user_id == user.user_id))
    ) == Decimal("250")
    rows = (
        await db.scalars(
            select(WalletTransaction).where(
                WalletTransaction.order_id == order.order_id
            )
        )
    ).all()
    assert len(rows) == 1 and rows[0].amount == Decimal("150")
    assert rows[0].reason == "rider_cash_change"
    assert (
        await db.get(
            CourierWalletCredit,
            change.source_reference(target, change.PendingCredit.model_validate(data)),
        )
        is not None
    )
    notify.assert_awaited_once()
    for wrong in [
        dict(data, merchant_order_ref="OTHER"),
        dict(data, cash_received="999"),
        dict(data, client_id=2),
        dict(data, amount="151", cash_received="1001"),
    ]:
        with pytest.raises(ValueError):
            await change.credit_request(db, target, wrong)
    other = CourierTarget(target.base_url, "other-account", "live")
    with pytest.raises(ValueError, match="does not own"):
        await change.credit_request(db, other, data)
    assert (
        await db.scalar(select(Wallet.balance).where(Wallet.user_id == user.user_id))
    ) == Decimal("250")


@pytest.mark.asyncio
async def test_missing_finance_outbox_blocks_credit(monkeypatch):
    monkeypatch.setattr(settings, "finance_outbox_enabled", False)
    with pytest.raises(RuntimeError, match="Finance outbox"):
        await change.credit_request(None, None, pending())


@pytest.mark.asyncio
async def test_worker_retries_lost_acknowledgement_and_continues_other_requests(
    monkeypatch,
):
    from contextlib import asynccontextmanager

    target = CourierTarget(
        "https://courier.example/api/v1/ecommerce", "test-key", "live"
    )
    transaction_id = uuid4()
    db = object()

    @asynccontextmanager
    async def session():
        yield db

    monkeypatch.setattr(change, "async_session_maker", session)
    credit = AsyncMock(return_value=transaction_id)
    monkeypatch.setattr(change, "credit_request", credit)
    failed_ack = False

    async def request(method, path, **kwargs):
        nonlocal failed_ack
        if path == "/wallet-change/pending":
            return [pending()]
        if path.endswith("/credited") and not failed_ack:
            failed_ack = True
            raise TimeoutError("Test acknowledgement timeout")
        return {"enabled": True}

    api = AsyncMock(side_effect=request)
    monkeypatch.setattr(change.CourierService, "_request", api)
    await change.process_targets([target])
    await change.process_targets([target])
    assert credit.await_count == 2
    acknowledgements = [
        call for call in api.await_args_list if call.args[1].endswith("/credited")
    ]
    assert len(acknowledgements) == 2
    assert acknowledgements[0].kwargs["json"] == acknowledgements[1].kwargs["json"]
    assert acknowledgements[0].kwargs["json"]["transaction_id"] == str(transaction_id)

    async def two_pending(method, path, **kwargs):
        return [pending(), pending(request_id=2)] if path.endswith("/pending") else {}

    api.side_effect = two_pending
    api.reset_mock()
    credit.side_effect = [ValueError("Invalid test request"), transaction_id]
    await change.process_targets([target])
    acknowledgements = [
        call for call in api.await_args_list if call.args[1].endswith("/credited")
    ]
    assert len(acknowledgements) == 1
    assert acknowledgements[0].args[1] == "/wallet-change/2/credited"
