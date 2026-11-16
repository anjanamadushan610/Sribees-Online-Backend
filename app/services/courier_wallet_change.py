"""Reconcile durable rider cash-change requests with the customer's wallet.

Express retains pending requests until it receives the committed wallet ledger
reference. A timeout before or after acknowledgement is safe to retry.
"""

import asyncio
from decimal import Decimal
from hashlib import sha256

from loguru import logger
from pydantic import BaseModel, Field
from sqlalchemy import select, text

from app.config.database import async_session_maker
from app.config.settings import settings
from app.models.order import Order
from app.models.wallet import CourierWalletCredit, WalletTransactionType
from app.schemas.notification import NotificationTypeEnum
from app.schemas.order import PaymentMethodEnum
from app.services import courier_credentials
from app.services.courier_service import CourierService
from app.services.finance_outbox import WalletReason
from app.services.notification_i18n import money
from app.services.notification_service import NotificationService
from app.services.wallet_service import WalletService


class PendingCredit(BaseModel):
    request_id: int = Field(gt=0)
    client_id: int = Field(gt=0)
    environment: str = Field(pattern="^(live|sandbox)$")
    shipment_id: str = Field(min_length=1, max_length=80)
    merchant_order_ref: str = Field(min_length=1, max_length=150)
    amount: Decimal = Field(gt=0, max_digits=12, decimal_places=2, allow_inf_nan=False)
    order_amount: Decimal = Field(
        gt=0, max_digits=12, decimal_places=2, allow_inf_nan=False
    )
    cash_received: Decimal = Field(
        gt=0, max_digits=12, decimal_places=2, allow_inf_nan=False
    )


def source_reference(target, request):
    host = sha256(target.base_url.rstrip("/").encode()).hexdigest()[:32]
    return (
        f"express:{host}:{request.client_id}:{request.environment}:{request.request_id}"
    )


async def credit_request(db, target, payload):
    if not settings.finance_outbox_enabled:
        raise RuntimeError("Finance outbox must be enabled for rider cash change")
    request = PendingCredit.model_validate(payload)
    if request.cash_received != request.order_amount + request.amount:
        raise ValueError("Courier cash amounts do not balance")
    order = await db.scalar(
        select(Order)
        .where(Order.courier_shipment_id == request.shipment_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if order is None or request.merchant_order_ref != order.order_number:
        raise ValueError("Courier request does not match an existing order")
    expected = await courier_credentials.resolve(db, order.branch_id)
    if (
        expected.base_url.rstrip("/") != target.base_url.rstrip("/")
        or expected.api_key != target.api_key
    ):
        raise ValueError("Courier account does not own this order")
    if expected.environment != request.environment:
        raise ValueError("Courier environment does not match")
    source = source_reference(target, request)
    existing = await db.get(CourierWalletCredit, source)
    if existing:
        if existing.order_id != order.order_id or existing.amount != request.amount:
            raise ValueError("Existing wallet credit differs")
        return existing.transaction_id
    other = await db.scalar(
        select(CourierWalletCredit).where(
            CourierWalletCredit.order_id == order.order_id
        )
    )
    if other:
        raise ValueError("Order already has a wallet change credit")
    if (
        str(order.payment_method).upper() != PaymentMethodEnum.COD.value
        or request.order_amount > order.total_amount
    ):
        raise ValueError("Wallet change requires a cash-on-delivery order")
    transaction = await WalletService.apply_transaction(
        db,
        order.user_id,
        WalletTransactionType.EARNED,
        request.amount,
        "Delivery cash change",
        order_id=order.order_id,
        order_number=order.order_number,
        notes=source,
        reason=WalletReason.RIDER_CASH_CHANGE,
    )
    db.add(
        CourierWalletCredit(
            source=source,
            order_id=order.order_id,
            transaction_id=transaction.transaction_id,
            amount=request.amount,
        )
    )
    await db.flush()
    await NotificationService.notify(
        db,
        order.user_id,
        template="rider_wallet_change",
        type=NotificationTypeEnum.PAYMENT,
        params={"amount": money(request.amount), "order_number": order.order_number},
        reference_type="wallet",
        reference_id=transaction.transaction_id,
        data={
            "type": "wallet_credit",
            "order_id": str(order.order_id),
            "amount": str(request.amount),
        },
    )
    await db.commit()
    return transaction.transaction_id


async def sweep_once():
    # Transaction-scoped lock works through PgBouncer and releases on crash.
    async with async_session_maker() as db:
        locked = await db.scalar(text("SELECT pg_try_advisory_xact_lock(7311205902)"))
        if not locked:
            return
        targets = await courier_credentials.all_targets(db)
        await process_targets(targets)


async def process_targets(targets):
    for target in targets:
        try:
            async with courier_credentials.using(target):
                await CourierService._request("POST", "/wallet-change/enable")
                pending = await CourierService._request("GET", "/wallet-change/pending")
                for payload in pending:
                    try:
                        async with async_session_maker() as db:
                            transaction_id = await credit_request(db, target, payload)
                        await CourierService._request(
                            "POST",
                            f"/wallet-change/{payload['request_id']}/credited",
                            json={
                                "transaction_id": str(transaction_id),
                                "amount": str(payload["amount"]),
                            },
                        )
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        logger.exception(
                            "Courier wallet change request remains pending"
                        )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning(
                "Courier wallet change reconciliation unavailable; pending cash credits retained"
            )


async def run_wallet_change_worker():
    if (
        settings.app_env.strip().lower() == "testing"
        or not settings.rider_wallet_change_enabled
    ):
        return
    while True:
        try:
            await sweep_once()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Courier wallet change sweep failed")
        await asyncio.sleep(15)
