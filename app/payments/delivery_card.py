"""COD-to-card at the door. Express owns the exclusion lock against cash.

Persist the intent before touching another service. Never unblock cash on a
browser cancellation, gateway timeout or age alone. The worker reconciles
gateway answers and retries Express acknowledgement after a crash/outage.
"""
from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

from loguru import logger
from sqlalchemy import func, select

from app.config.settings import settings
from app.models.order import Order, PaymentStatus
from app.models.payment import PaymentAttempt
from app.payments import catalog, texts
from app.payments.gateways import get_gateway
from app.payments.gateways.base import (
    GatewayAnswer,
    GatewayError,
    Outcome,
    PayerDetailsMissing,
)
from app.services.courier_service import (
    CourierError,
    CourierService,
    CourierUnavailableError,
)

PURPOSE = "delivery_card"


def payment_view(order, lang):
    state = getattr(order, "delivery_card_state", None)
    offered = (settings.delivery_card_payment_enabled
               and (order.payment_method or "").upper() == "COD"
               and order.status == "out_for_delivery"
               and order.payment_status != "paid" and order.courier_shipment_id
               and not order.courier_cod_collected and Decimal(str(order.total_amount or 0)) > 0
               and catalog.get_method("MARX_OTHER").enabled())
    if not state and not offered:
        return None
    if state == "failed" and not offered:
        return None
    pending = state in ("preparing", "pending", "syncing", "review")
    paid = state == "paid"
    key = "door.paid" if paid else "door.pending" if pending else "door.available"
    return {
        "method": {"code": order.payment_method, "name": "Visa / Mastercard / UnionPay", "icon": "card"},
        "state": "delivery_card_paid" if paid else "delivery_card_pending" if pending else "on_delivery",
        "title": texts.text(key, lang), "message": texts.text(key + ".message", lang),
        "tone": "success" if paid else "warning" if pending else "info",
        "can_pay": bool(offered and state not in ("preparing", "syncing", "review", "paid")),
        "pay_label": texts.text("door.resume" if state == "pending" else "door.pay", lang), "pay_by": None,
    }


def action(attempt):
    return {"type": "redirect", "url": attempt.checkout_url,
            "return_url": get_gateway(attempt.gateway).return_url,
            "complete_path": f"/payments/orders/{attempt.order_id}/complete"}


async def lock_order(db, order_id):
    return (await db.execute(select(Order).where(Order.order_id == order_id).with_for_update()
                            .execution_options(populate_existing=True))).scalar_one()


async def start(db, order, user, *, lang):
    from app.payments.service import PaymentError, _now, open_checkout, payer_for

    order = await lock_order(db, order.order_id)
    if not settings.delivery_card_payment_enabled:
        raise PaymentError(texts.text("error.methodUnavailable", lang))
    view = payment_view(order, lang)
    # A second tap resumes the SAME checkout, never another card charge.
    if order.delivery_card_reference and order.delivery_card_state in ("preparing", "pending"):
        previous = await db.scalar(select(PaymentAttempt).where(
            PaymentAttempt.reference == order.delivery_card_reference))
        if previous is not None and previous.checkout_url and previous.status in ("started", "pending"):
            return action(previous)
        raise PaymentError(texts.text("door.pending.message", lang), 409)
    if view is None or not view["can_pay"]:
        raise PaymentError(texts.text("error.notPayable", lang), 409)
    if not getattr(user, "email", None):
        raise PaymentError(texts.text("door.email", lang))
    tries = await db.scalar(select(func.count()).select_from(PaymentAttempt).where(
        PaymentAttempt.order_id == order.order_id))
    if tries >= settings.payment_max_attempts:
        raise PaymentError(texts.text("error.tooManyTries", lang))
    method = catalog.get_method("MARX_OTHER")
    attempt = PaymentAttempt(
        attempt_id=uuid4(), order_id=order.order_id, attempt_no=tries + 1,
        method_code=method.code, gateway=method.gateway, reference=f"DOOR-{uuid4().hex}",
        amount=order.total_amount, purpose=PURPOSE, status="started", created_at=_now(),
    )
    db.add(attempt)
    order.delivery_card_reference = attempt.reference
    order.delivery_card_state = "preparing"
    await db.commit()
    # Keep the online order lock during the bounded reserve/start calls so the
    # recovery worker cannot abandon an in-flight start. The committed intent
    # survives process death before/after either remote call.
    await db.execute(select(PaymentAttempt).where(PaymentAttempt.attempt_id == attempt.attempt_id)
                     .with_for_update())
    order = await lock_order(db, order.order_id)
    try:
        await CourierService.delivery_card_payment(
            order.courier_shipment_id, operation="reserve", reference=attempt.reference,
            amount=str(attempt.amount),
        )
        checkout = await open_checkout(method=method, reference=attempt.reference,
                                       amount=attempt.amount, payer=payer_for(user),
                                       order_number=order.order_number)
        if not checkout.gateway_ref:
            raise GatewayError("Gateway reference missing")
        attempt.gateway_ref = checkout.gateway_ref
        attempt.checkout_url = checkout.redirect_url
        order.delivery_card_state = "pending"
        await db.commit()
    except (CourierError, CourierUnavailableError, GatewayError, PayerDetailsMissing) as exc:
        # No checkout URL has been handed out. A durable failed start is
        # released by reconciliation; a lost reserve response is not guessed.
        attempt.status = "failed"
        attempt.completed_at = _now()
        order.delivery_card_state = "syncing"
        await db.commit()
        await sync_courier(db, order, attempt)
        logger.warning(f"Delivery card checkout could not start for {order.order_id}: {type(exc).__name__}")
        raise PaymentError(texts.text("error.gateway", lang), 502) from exc
    return action(attempt)


async def sync_courier(db, order, attempt):
    if attempt.courier_synced == "yes":
        return
    operation = "paid" if attempt.status == "paid" else "failed"
    try:
        answer = await CourierService.delivery_card_payment(
            order.courier_shipment_id, operation=operation, reference=attempt.reference,
            amount=str(attempt.amount), gateway_reference=attempt.gateway_ref,
        )
        if answer.get("state") != operation:
            raise CourierUnavailableError("Unexpected delivery payment acknowledgement")
    except (CourierError, CourierUnavailableError):
        logger.warning(f"Delivery card acknowledgement pending for {attempt.reference}; cash remains blocked")
        return
    attempt.courier_synced = "yes"
    if order.delivery_card_reference == attempt.reference:
        order.delivery_card_state = operation
    await db.commit()


async def apply_answer(db, order, attempt, answer):
    from app.payments.service import _now

    if attempt.status in ("paid", "failed"):
        # A definitive failure must never later silently become COD + card.
        if attempt.status == "failed" and answer.outcome == Outcome.PAID:
            from app.payments.service import _give_back

            # Follow the existing duplicate/late payment policy: retain COD,
            # credit the unexpected payment back to the customer's wallet once.
            attempt.status = "paid"
            await _give_back(db, order, attempt, second=True)
            attempt.raw = {**(answer.raw or {}), "delivery_refunded_to_wallet": True}
            attempt.courier_synced = "yes"
            await db.commit()
            return Outcome.PAID
        await db.commit()
        await sync_courier(db, order, attempt)
        return Outcome.PAID if attempt.status == "paid" else Outcome.FAILED
    if answer.code == "UNOPENED_START" and attempt.gateway_ref:
        await db.commit()
        return Outcome.PENDING
    attempt.raw = answer.raw or None
    attempt.gateway_code = (answer.code or "")[:80] or None
    if answer.outcome == Outcome.PENDING:
        attempt.status = "pending"
        await db.commit()
        return Outcome.PENDING
    attempt.status = answer.outcome.value
    attempt.completed_at = _now()
    order.delivery_card_state = "syncing"
    if answer.outcome == Outcome.PAID:
        # Same transaction as payment.received's existing Finance outbox hook.
        # Never re-confirm/re-book the already-dispatched order or its stock.
        order.payment_method = attempt.method_code
        order.payment_status = PaymentStatus.PAID.value
        order.payment_id = attempt.gateway_ref
        order.gateway_tr_id = attempt.gateway_ref
        order.gateway_status = "SUCCESS"
        order.gateway_response_code = (answer.code or "")[:50] or None
        order.gateway_confirmed_at = _now()
        order.gateway_raw_response = answer.raw or None
    await db.commit()
    await sync_courier(db, order, attempt)
    return answer.outcome


async def sweep(db, now):
    from app.payments.service import _aware
    from app.payments.service import apply_answer as apply_payment_answer

    ids = (await db.execute(select(PaymentAttempt.attempt_id).where(
        PaymentAttempt.purpose == PURPOSE,
        (PaymentAttempt.courier_synced.is_(None)) | (PaymentAttempt.status.in_(("started", "pending"))),
    ).order_by(PaymentAttempt.created_at))).scalars().all()
    for attempt_id in ids:
        try:
            attempt = await db.get(PaymentAttempt, attempt_id)
            if attempt.status in ("paid", "failed"):
                answer = GatewayAnswer(Outcome(attempt.status))
            elif attempt.gateway_ref:
                answer = await get_gateway(attempt.gateway).status(
                    reference=attempt.reference, gateway_ref=attempt.gateway_ref)
            elif _aware(attempt.created_at) < now - timedelta(minutes=5):
                # The locked apply path rechecks that no URL was committed.
                answer = GatewayAnswer(Outcome.FAILED, code="UNOPENED_START")
            else:
                await db.commit()
                continue
            await apply_payment_answer(db, attempt_id, answer)
        except Exception:
            await db.rollback()
            logger.exception(f"Delivery card recovery pending for {attempt_id}")
