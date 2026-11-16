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
    money_db, monkeypatch  # noqa: F811 - imported shared pytest fixture
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
