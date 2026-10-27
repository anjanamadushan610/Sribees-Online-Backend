"""
Online payments: an order that waits for its money.

An order paid by card or bank transfer is created ``awaiting_payment``. Its
stock, coupon and wallet money are held, but it is not placed: the branch
cannot act on it and "order placed" is not sent. Then one of three things:

  paid       the gateway confirms a try -> CONFIRMED, "order placed" goes out,
             rewards and the delivery code follow (OrderService's usual
             just-paid path).
  declined   the try is marked failed and the order says "Payment failed";
             the customer can pay again from their orders, by any online
             method, until the hold closes (settings.payment_hold_minutes).
  nothing    the customer walked away. When the hold closes -- plus a grace
             for a try still in flight -- the sweep asks the gateway one last
             time and otherwise cancels the order, releasing everything held.

Every try is a PaymentAttempt. Our reference identifies it to the gateway; the
first is the order number (as before), a retry is ``<order number>-<n>``. The
return from the gateway passes through the customer's browser, so it is only
used to find WHICH try to ask about -- the answer always comes from the
gateway itself, server to server.

Money that arrives when it should not -- a second try paid after the first,
or a try paid after the order was cancelled -- is credited back to the
customer's wallet at once and logged. Nobody pays twice and keeps nothing.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Optional
from urllib.parse import parse_qsl, urlsplit
from zoneinfo import ZoneInfo

from loguru import logger
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import settings
from app.models.order import Order, OrderStatus, PaymentStatus
from app.models.payment import (
    OPEN_ATTEMPT_STATUSES,
    PaymentAttempt,
    PaymentAttemptStatus,
)
from app.models.wallet import WalletTransactionType
from app.payments import catalog, texts
from app.payments.gateways import get_gateway
from app.payments.gateways.base import (
    Checkout,
    GatewayAnswer,
    GatewayError,
    Outcome,
    Payer,
    PayerDetailsMissing,
)
from app.services.finance_outbox import WalletReason

COLOMBO = ZoneInfo("Asia/Colombo")

# A try younger than this is left alone: the customer may still be on the page.
_SETTLE_AFTER = timedelta(minutes=2)
# A try nobody has answered for this long is given up on.
_GIVE_UP_AFTER = timedelta(hours=24)


class PaymentError(ValueError):
    """A refusal the customer reads, already in their language."""

    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class CompleteResult:
    outcome: Outcome
    order: Order
    message: str


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def pay_by(order: Order) -> Optional[datetime]:
    """Until when the customer may pay (or pay again) for this order."""
    created = _aware(order.created_at)
    return created + timedelta(minutes=settings.payment_hold_minutes) if created else None


def payer_for(user) -> Payer:
    return Payer(
        email=getattr(user, "email", None),
        phone=getattr(user, "phone", None),
        name=getattr(user, "full_name", None),
    )


def action_for(order: Order, redirect_url: str) -> dict:
    """What the app does next: open the page, watch for the way back."""
    method = catalog.get_method(order.payment_method)
    gateway = get_gateway(method.gateway) if method and method.gateway else None
    return {
        "type": "redirect",
        "url": redirect_url,
        "return_url": gateway.return_url if gateway else None,
        "complete_path": f"/payments/orders/{order.order_id}/complete",
    }


# ---------------------------------------------------------------------------
# What the customer sees
# ---------------------------------------------------------------------------


def _clock(value: datetime, lang: str) -> str:
    local = value.astimezone(COLOMBO)
    hour = local.hour % 12 or 12
    suffix = "AM" if local.hour < 12 else "PM"
    return f"{hour}:{local.minute:02d} {suffix}"


def status_label(order: Order, lang: str) -> str:
    if (
        order.status == OrderStatus.AWAITING_PAYMENT.value
        and order.payment_status == PaymentStatus.FAILED.value
    ):
        return texts.status_label("payment_failed", lang)
    return texts.status_label(order.status, lang)


def payment_view(order: Order, lang: str, now: Optional[datetime] = None) -> dict:
    """The order's payment as the app draws it: a title, a line, a tone, and
    whether to offer "Pay now". Decided here so the app needs no rules."""
    now = now or _now()
    method = catalog.get_method(order.payment_method)
    view = {
        "method": {
            "code": order.payment_method,
            "name": method.name if method else (order.payment_method or ""),
            "icon": method.icon if method else "other",
        },
        "state": "paid",
        "title": texts.text("paid.title", lang),
        "message": None,
        "tone": "success",
        "can_pay": False,
        "pay_label": None,
        "pay_by": None,
    }
    status = order.status
    paid = order.payment_status == PaymentStatus.PAID.value

    if order.payment_status == PaymentStatus.REFUNDED.value:
        view.update(state="refunded", title=texts.text("refunded.title", lang), tone="info")
        return view

    if method is not None and method.kind == "offline" and not paid:
        view.update(state="on_delivery", title=texts.text("onDelivery.title", lang), tone="info")
        return view

    if status == OrderStatus.AWAITING_PAYMENT.value and not paid:
        deadline = pay_by(order)
        open_ = deadline is not None and now < deadline
        failed = order.payment_status == PaymentStatus.FAILED.value
        when = _clock(deadline, lang) if deadline else ""
        if failed:
            view.update(
                state="failed",
                title=texts.status_label("payment_failed", lang),
                message=texts.text("failed.message", lang, time=when) if open_ else None,
                tone="error",
                pay_label=texts.text("tryAgain", lang),
            )
        else:
            message = texts.text("processing.message", lang)
            if open_:
                message = f"{message} {texts.text('processing.payBy', lang, time=when)}"
            view.update(
                state="processing",
                title=texts.status_label("awaiting_payment", lang),
                message=message,
                tone="warning",
                pay_label=texts.text("payNow", lang),
            )
        view.update(can_pay=open_, pay_by=deadline.isoformat() if deadline else None)
        return view

    if status == OrderStatus.CANCELLED.value and method is not None and method.is_online and not paid:
        view.update(
            state="not_completed",
            title=texts.text("notCompleted.title", lang),
            message=texts.text("notCompleted.message", lang),
            tone="info",
        )
        return view

    if not paid:
        view.update(state="pending", title=texts.status_label("pending", lang), tone="info")
    return view


# ---------------------------------------------------------------------------
# Starting a try
# ---------------------------------------------------------------------------


async def open_checkout(
    *, method: catalog.MethodSpec, reference: str, amount: Decimal, payer: Payer, order_number: str
) -> Checkout:
    """Ask the method's gateway for a payment page. Nothing is written."""
    gateway = get_gateway(method.gateway)
    return await gateway.start(
        reference=reference,
        amount=amount,
        option=method.option,
        payer=payer,
        description=f"SRIBEESonline order {order_number}",
    )


def stage_attempt(
    db: AsyncSession,
    *,
    order: Order,
    attempt_no: int,
    method: catalog.MethodSpec,
    reference: str,
    checkout: Checkout,
    amount: Decimal,
) -> PaymentAttempt:
    """Record a try in the caller's transaction."""
    attempt = PaymentAttempt(
        order_id=order.order_id,
        attempt_no=attempt_no,
        method_code=method.code,
        gateway=method.gateway,
        reference=reference,
        gateway_ref=checkout.gateway_ref,
        amount=amount,
        status=PaymentAttemptStatus.STARTED.value,
    )
    db.add(attempt)
    return attempt


async def pay_again(
    db: AsyncSession, order: Order, user, *, method_code: Optional[str], lang: str
) -> dict:
    """A new try for an order still waiting for its money."""
    if order.status != OrderStatus.AWAITING_PAYMENT.value or order.payment_status == PaymentStatus.PAID.value:
        raise PaymentError(texts.text("error.notPayable", lang))
    deadline = pay_by(order)
    if deadline is None or _now() >= deadline:
        raise PaymentError(texts.text("error.notPayable", lang))

    method = catalog.get_method(method_code or order.payment_method)
    if method is None or not method.is_online or not method.enabled():
        raise PaymentError(texts.text("error.methodUnavailable", lang))

    tries = await db.scalar(
        select(func.count()).select_from(PaymentAttempt).where(PaymentAttempt.order_id == order.order_id)
    )
    if tries >= settings.payment_max_attempts:
        raise PaymentError(texts.text("error.tooManyTries", lang))

    attempt_no = tries + 1
    reference = f"{order.order_number}-{attempt_no}"
    try:
        checkout = await open_checkout(
            method=method,
            reference=reference,
            amount=order.total_amount,
            payer=payer_for(user),
            order_number=order.order_number,
        )
    except PayerDetailsMissing as exc:
        raise PaymentError(str(exc)) from exc
    except GatewayError as exc:
        logger.warning(f"Payment retry for {order.order_number} could not start: {exc}")
        raise PaymentError(texts.text("error.gateway", lang), status_code=502) from exc

    # Earlier tries stay on the books -- the customer may yet have paid one --
    # but they are no longer the one the order points at.
    await db.execute(
        update(PaymentAttempt)
        .where(
            PaymentAttempt.order_id == order.order_id,
            PaymentAttempt.status.in_(
                (PaymentAttemptStatus.STARTED.value, PaymentAttemptStatus.PENDING.value)
            ),
        )
        .values(status=PaymentAttemptStatus.SUPERSEDED.value)
    )
    stage_attempt(
        db,
        order=order,
        attempt_no=attempt_no,
        method=method,
        reference=reference,
        checkout=checkout,
        amount=order.total_amount,
    )
    order.payment_method = method.code
    order.gateway_tr_id = checkout.gateway_ref
    order.payment_status = PaymentStatus.PENDING.value
    await db.commit()
    await db.refresh(order)
    logger.info(f"Payment try {attempt_no} started for {order.order_number} by {method.code}")
    return action_for(order, checkout.redirect_url)


# ---------------------------------------------------------------------------
# Finishing a try
# ---------------------------------------------------------------------------


def return_params(return_url: str) -> dict:
    """The query of the URL the app landed on. Only the query is read."""
    return dict(parse_qsl(urlsplit(return_url or "").query))


async def complete(db: AsyncSession, order: Order, params: dict, lang: str) -> CompleteResult:
    """The customer is back from the gateway's page: find the try, ask the
    gateway how it went, and act on its answer."""
    method = catalog.get_method(order.payment_method)
    attempts = (
        await db.execute(select(PaymentAttempt).where(PaymentAttempt.order_id == order.order_id))
    ).scalars().all()
    gateways = {a.gateway for a in attempts} or ({method.gateway} if method and method.gateway else set())

    attempt = None
    for name in gateways:
        parsed = get_gateway(name).parse_return(params)
        if parsed is None:
            continue
        reference, gateway_ref = parsed
        attempt = next(
            (a for a in attempts if a.reference == reference and a.gateway == name and a.gateway_ref == gateway_ref),
            None,
        )
        if attempt is not None:
            break
    if attempt is None:
        raise PaymentError(texts.text("error.returnMismatch", lang))

    if attempt.status == PaymentAttemptStatus.PAID.value:
        return CompleteResult(Outcome.PAID, order, texts.text("result.paid", lang))

    try:
        answer = await get_gateway(attempt.gateway).settle(
            reference=attempt.reference, gateway_ref=attempt.gateway_ref
        )
    except GatewayError as exc:
        logger.warning(f"Could not settle {attempt.reference} with {attempt.gateway}: {exc}")
        raise PaymentError(texts.text("error.gateway", lang), status_code=502) from exc

    outcome = await apply_answer(db, attempt.attempt_id, answer)
    fresh = await db.get(Order, order.order_id, populate_existing=True)
    key = {Outcome.PAID: "result.paid", Outcome.FAILED: "result.failed"}.get(outcome, "result.pending")
    return CompleteResult(outcome, fresh, texts.text(key, lang))


async def apply_answer(db: AsyncSession, attempt_id, answer: GatewayAnswer) -> Outcome:
    """Record what the gateway said about one try, and move the order.

    Both rows are locked, so a customer's return racing the sweep -- or two
    tries answering at once -- settle one after the other, never twice.
    """
    from app.services.order_service import OrderService

    attempt = (
        await db.execute(
            select(PaymentAttempt).where(PaymentAttempt.attempt_id == attempt_id).with_for_update()
        )
    ).scalar_one()
    order = (
        await db.execute(
            select(Order)
            .where(Order.order_id == attempt.order_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one()

    if attempt.status == PaymentAttemptStatus.PAID.value:
        await db.commit()
        return Outcome.PAID

    attempt.raw = answer.raw or None
    attempt.gateway_code = (answer.code or "")[:80] or None

    # The order's own gateway columns follow the try it points at, as before,
    # for the admin screens and support.
    if order.gateway_tr_id == attempt.gateway_ref:
        order.gateway_status = {
            Outcome.PAID: "SUCCESS", Outcome.FAILED: "FAILURE"
        }.get(answer.outcome, "PENDING")
        order.gateway_response_code = (answer.code or "")[:50] or None
        order.gateway_raw_response = answer.raw or None
        order.gateway_confirmed_at = datetime.utcnow()

    if answer.outcome == Outcome.PAID:
        attempt.status = PaymentAttemptStatus.PAID.value
        attempt.completed_at = _now()
        already_paid = order.payment_status == PaymentStatus.PAID.value
        if already_paid or order.status == OrderStatus.CANCELLED.value:
            await _give_back(db, order, attempt, second=already_paid)
            await db.commit()
            return Outcome.PAID
        order.gateway_tr_id = attempt.gateway_ref
        await db.commit()
        await OrderService.update_payment_status(
            db, order, _paid(), payment_id=attempt.gateway_ref
        )
        return Outcome.PAID

    if answer.outcome == Outcome.FAILED:
        attempt.status = PaymentAttemptStatus.FAILED.value
        attempt.completed_at = _now()
        still_waiting = (
            order.status == OrderStatus.AWAITING_PAYMENT.value
            or order.status == OrderStatus.PENDING.value
        )
        if still_waiting and order.payment_status != PaymentStatus.PAID.value and order.gateway_tr_id == attempt.gateway_ref:
            order.payment_status = PaymentStatus.FAILED.value
        await db.commit()
        return Outcome.FAILED

    if attempt.status == PaymentAttemptStatus.STARTED.value:
        attempt.status = PaymentAttemptStatus.PENDING.value
    await db.commit()
    return Outcome.PENDING


def _paid():
    from app.schemas.order import PaymentStatusEnum

    return PaymentStatusEnum.PAID


async def _give_back(db: AsyncSession, order: Order, attempt: PaymentAttempt, *, second: bool) -> None:
    """Money that arrived for an order that no longer wants it: straight back
    to the customer's wallet. Staged in the caller's transaction."""
    from app.services.wallet_service import WalletService

    why = "a second payment for an order already paid" if second else "a payment for a cancelled order"
    await WalletService.apply_transaction(
        db,
        user_id=order.user_id,
        tx_type=WalletTransactionType.REFUND,
        amount=Decimal(str(attempt.amount)),
        title="Payment refund",
        reason=WalletReason.PAYMENT_REFUND,
        order_id=order.order_id,
        order_number=order.order_number,
        notes=f"Refunded {why} ({attempt.reference})",
    )
    if not second:
        order.payment_status = PaymentStatus.REFUNDED.value
        order.refund_amount = (order.refund_amount or Decimal("0")) + Decimal(str(attempt.amount))
        order.refunded_at = datetime.utcnow()
    logger.warning(
        f"{attempt.reference}: {why}; Rs {attempt.amount} credited to the customer's wallet"
    )


# ---------------------------------------------------------------------------
# The sweep
# ---------------------------------------------------------------------------


async def _adopt_legacy(db: AsyncSession) -> None:
    """Gateway orders made before tries were recorded (or by an old build
    during a deploy) get their first try on record, so they settle too."""
    online = [m.code for m in catalog.METHODS if m.is_online]
    rows = (
        await db.execute(
            select(Order).where(
                Order.payment_method.in_(online),
                Order.payment_status == PaymentStatus.PENDING.value,
                Order.gateway_tr_id.isnot(None),
                ~select(PaymentAttempt.attempt_id)
                .where(PaymentAttempt.order_id == Order.order_id)
                .exists(),
            )
        )
    ).scalars().all()
    for order in rows:
        method = catalog.get_method(order.payment_method)
        db.add(
            PaymentAttempt(
                order_id=order.order_id,
                attempt_no=1,
                method_code=method.code,
                gateway=method.gateway,
                reference=order.order_number,
                gateway_ref=order.gateway_tr_id,
                amount=order.total_amount,
                status=PaymentAttemptStatus.STARTED.value,
                created_at=order.created_at,
            )
        )
    if rows:
        await db.commit()


async def sweep(db: AsyncSession, now: Optional[datetime] = None) -> dict:
    """Settle tries whose customer never came back, and close orders whose
    payment window has passed. Safe to run as often as you like."""
    from app.services.notification_service import NotificationService
    from app.services.order_service import OrderService

    now = now or _now()
    result = {"checked": 0, "paid": 0, "failed": 0, "still_pending": 0, "expired_orders": 0}

    await _adopt_legacy(db)

    open_tries = (
        await db.execute(
            select(PaymentAttempt.attempt_id, PaymentAttempt.gateway, PaymentAttempt.reference,
                   PaymentAttempt.gateway_ref, PaymentAttempt.created_at)
            .where(
                PaymentAttempt.status.in_(OPEN_ATTEMPT_STATUSES),
                PaymentAttempt.gateway_ref.isnot(None),
                PaymentAttempt.created_at < now - _SETTLE_AFTER,
            )
            .order_by(PaymentAttempt.created_at)
        )
    ).all()
    for attempt_id, gateway, reference, gateway_ref, created_at in open_tries:
        if _aware(created_at) < now - _GIVE_UP_AFTER:
            await db.execute(
                update(PaymentAttempt)
                .where(PaymentAttempt.attempt_id == attempt_id)
                .values(status=PaymentAttemptStatus.EXPIRED.value, completed_at=now)
            )
            await db.commit()
            continue
        result["checked"] += 1
        try:
            answer = await get_gateway(gateway).status(reference=reference, gateway_ref=gateway_ref)
        except GatewayError as exc:
            logger.warning(f"Payment sweep: no answer for {reference}: {exc}")
            result["still_pending"] += 1
            continue
        outcome = await apply_answer(db, attempt_id, answer)
        result[{Outcome.PAID: "paid", Outcome.FAILED: "failed"}.get(outcome, "still_pending")] += 1

    hold = timedelta(minutes=settings.payment_hold_minutes)
    grace = timedelta(minutes=settings.payment_attempt_grace_minutes)
    overdue = (
        await db.execute(
            select(Order.order_id).where(
                Order.status == OrderStatus.AWAITING_PAYMENT.value,
                Order.created_at < now - hold,
            )
        )
    ).scalars().all()
    for order_id in overdue:
        latest = await db.scalar(
            select(func.max(PaymentAttempt.created_at)).where(PaymentAttempt.order_id == order_id)
        )
        if latest is not None and _aware(latest) > now - grace:
            continue  # a try is still in flight
        # Every server process runs this sweep. Whoever locks the row first
        # cancels it; the others skip it rather than wait and cancel twice.
        locked = await db.scalar(
            select(Order.order_id)
            .where(Order.order_id == order_id, Order.status == OrderStatus.AWAITING_PAYMENT.value)
            .with_for_update(skip_locked=True)
        )
        if locked is None:
            await db.rollback()
            continue
        order = await OrderService.get_by_id(db, order_id)
        if order is None or order.status != OrderStatus.AWAITING_PAYMENT.value:
            await db.rollback()
            continue
        if order.payment_status == PaymentStatus.PAID.value:
            continue
        await OrderService.cancel_order(
            db, order, reason="Payment was not completed in time", cancelled_by="system"
        )
        result["expired_orders"] += 1
        logger.info(f"{order.order_number}: payment window closed unpaid; order cancelled")
        try:
            await NotificationService.notify_order_status(
                db,
                user_id=order.user_id,
                order_number=order.order_number,
                status=OrderStatus.CANCELLED.value,
                order_id=order.order_id,
            )
        except Exception as exc:  # pragma: no cover - a notice never undoes a cancel
            logger.warning(f"Could not tell {order.order_number}'s customer it was cancelled: {exc}")

    if any(result.values()):
        logger.info(f"Payment sweep: {result}")
    return result
