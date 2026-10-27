"""
Finance outbox: what Sribees Finance needs to keep the books (migration 093).

Sribees Finance is a separate app with its own database. It never reads ours;
it pulls these events through ``GET /internal/finance/events`` and posts a
journal for each. Every event is written in the SAME transaction as the change
it describes, so the books can never hear of something that rolled back, nor
miss something that committed.

Four kinds, each a full snapshot of the figures (Finance never calls back):

* ``order.status``      -- every status change of an order, with its amounts,
                           lines and, when it is refunded, the returned lines.
                           Caught by a flush hook, so no code path that sets
                           ``Order.status`` can skip it.
* ``payment.received``  -- a gateway try that came back paid, whatever then
                           happens to the money.
* ``wallet.movement``   -- every wallet credit/debit with its ``reason``
                           (the ledger accounts depend on why, not on how much).
* ``commission.changed``-- a referral commission row changing status or amount.

Off unless ``FINANCE_OUTBOX_ENABLED`` (ships dark). Writing an event must
never break the change itself: a failure to build a payload is logged and the
write goes ahead without it, and Finance's daily reconciliation catches the
gap.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Optional

from loguru import logger
from sqlalchemy import event, inspect, select
from sqlalchemy.orm import Session

from app.config.settings import settings
from app.models.branch import Branch
from app.models.finance_outbox import FinanceOutbox
from app.models.order import Order, OrderItem
from app.models.payment import PaymentAttempt, PaymentAttemptStatus
from app.models.product import ProductVariant
from app.models.referral import ReferralCommission


class WalletReason:
    """Why a wallet moved. Finance maps each to ledger accounts; a new reason
    needs a posting rule there before it is used here."""

    WALLET_PAYMENT = "wallet_payment"                # paid part of an order
    CANCEL_REFUND = "cancel_refund"                  # order cancelled, money back
    CANCEL_DELIVERY_CHARGE = "cancel_delivery_charge"  # fee kept on a booked cancel
    RETURN_REFUND = "return_refund"                  # goods returned after delivery
    PAYMENT_REFUND = "payment_refund"                # late or double gateway money
    CASHBACK_CREDIT = "cashback_credit"              # held cashback released
    CASHBACK_REVERSAL = "cashback_reversal"          # released cashback taken back
    COMMISSION_CREDIT = "commission_credit"          # referral commission claimed
    COMMISSION_REVERSAL = "commission_reversal"      # paid commission taken back

    ALL = frozenset({
        WALLET_PAYMENT, CANCEL_REFUND, CANCEL_DELIVERY_CHARGE, RETURN_REFUND,
        PAYMENT_REFUND, CASHBACK_CREDIT, CASHBACK_REVERSAL, COMMISSION_CREDIT,
        COMMISSION_REVERSAL,
    })


def enabled() -> bool:
    return bool(settings.finance_outbox_enabled)


def _money(v: Any) -> str:
    return f"{Decimal(str(v or 0)).quantize(Decimal('0.01'))}"


def _iso(v: Optional[datetime]) -> Optional[str]:
    if v is None:
        return None
    if v.tzinfo is None:  # a few columns are naive UTC
        v = v.replace(tzinfo=timezone.utc)
    return v.isoformat()


def _row(event_type: str, aggregate_id: Optional[uuid.UUID], payload: dict) -> FinanceOutbox:
    return FinanceOutbox(
        event_id=uuid.uuid4(),
        event_type=event_type,
        aggregate_id=aggregate_id,
        occurred_at=datetime.now(timezone.utc),
        payload=payload,
    )


# ---------------------------------------------------------------------------
# Wallet movements (called from WalletService.apply_transaction)
# ---------------------------------------------------------------------------


async def wallet_movement(db, transaction, reason: Optional[str]) -> None:
    """Stage one ``wallet.movement``. ``db`` is the caller's AsyncSession."""
    if not enabled():
        return
    try:
        branch_id = None
        if transaction.order_id is not None:
            branch_id = (
                await db.execute(select(Order.branch_id).where(Order.order_id == transaction.order_id))
            ).scalar_one_or_none()
        if reason not in WalletReason.ALL:
            logger.warning(f"finance_outbox: wallet movement {transaction.transaction_id} has no known reason ({reason})")
        db.add(_row("wallet.movement", transaction.order_id, {
            "transaction_id": str(transaction.transaction_id),
            "user_id": str(transaction.user_id),
            "order_id": str(transaction.order_id) if transaction.order_id else None,
            "order_number": transaction.order_number,
            "branch_id": str(branch_id) if branch_id else None,
            "type": transaction.type,
            "reason": reason,
            "amount": _money(transaction.amount),
            "balance_after": _money(transaction.balance_after),
            "title": transaction.title,
        }))
    except Exception as exc:  # noqa: BLE001 -- the wallet write must go ahead
        logger.error(f"finance_outbox: wallet movement not recorded: {exc}")


# ---------------------------------------------------------------------------
# Flush hook: order status, payment tries, commissions
# ---------------------------------------------------------------------------


def _changed(obj, *names: str) -> bool:
    state = inspect(obj)
    return any(state.attrs[n].history.has_changes() for n in names)


def _old(obj, name: str):
    hist = inspect(obj).attrs[name].history
    return hist.deleted[0] if hist.deleted else None


def _order_lines(session: Session, order: Order) -> list[dict]:
    rows = session.connection().execute(
        select(
            OrderItem.order_item_id, OrderItem.product_id, OrderItem.variant_id,
            OrderItem.product_name, OrderItem.product_sku, OrderItem.variant_name,
            OrderItem.quantity, OrderItem.unit_price, OrderItem.subtotal,
            OrderItem.coupon_discount, ProductVariant.weight_grams,
        )
        .outerjoin(ProductVariant, ProductVariant.variant_id == OrderItem.variant_id)
        .where(OrderItem.order_id == order.order_id)
        .order_by(OrderItem.created_at, OrderItem.order_item_id)
    ).all()
    return [
        {
            "order_item_id": str(r.order_item_id),
            "product_id": str(r.product_id),
            "variant_id": str(r.variant_id) if r.variant_id else None,
            "name": r.product_name,
            "sku": r.product_sku,
            "variant_name": r.variant_name,
            # grams per unit for a sold-by-weight option, else null
            "weight_grams": r.weight_grams,
            "quantity": int(r.quantity or 0),
            "unit_price": _money(r.unit_price),
            "subtotal": _money(r.subtotal),
            "coupon_discount": _money(r.coupon_discount),
        }
        for r in rows
    ]


def _returned(order: Order, lines: list[dict]) -> list[dict]:
    """Same rule as OrderService._returned_lines: no return_items = everything."""
    wanted = order.return_items or []
    if not wanted:
        return [{"order_item_id": ln["order_item_id"], "quantity": ln["quantity"]}
                for ln in lines if ln["quantity"] > 0]
    by_id = {ln["order_item_id"]: ln for ln in lines}
    out = []
    for ri in wanted:
        line = by_id.get(str(ri.get("order_item_id")))
        if line is None:
            continue
        qty = min(int(ri.get("quantity", 0) or 0), line["quantity"])
        if qty > 0:
            out.append({"order_item_id": line["order_item_id"], "quantity": qty})
    return out


def _order_event(session: Session, order: Order, old_status: Optional[str]) -> FinanceOutbox:
    branch = None
    if order.branch_id is not None:
        branch = session.connection().execute(
            select(Branch.branch_id, Branch.code, Branch.name).where(Branch.branch_id == order.branch_id)
        ).first()
    lines = _order_lines(session, order) if order.order_id is not None else []
    payload = {
        "order_id": str(order.order_id),
        "order_number": order.order_number,
        "user_id": str(order.user_id),
        "branch": {"branch_id": str(branch.branch_id), "code": branch.code, "name": branch.name}
        if branch else None,
        "from_status": old_status,
        "to_status": order.status or "pending",
        "payment_method": order.payment_method,
        "payment_status": order.payment_status,
        "amounts": {
            "subtotal": _money(order.subtotal),
            "discount": _money(order.discount_amount),
            "shipping": _money(order.shipping_amount),
            "tax": _money(order.tax_amount),
            "total": _money(order.total_amount),
            "wallet_deduction": _money(order.wallet_deduction),
            "cashback_earned": _money(order.cashback_earned),
            # what SribeesExpress charges us for this parcel
            "courier_charge": _money(order.courier_quote_amount),
            "refund": _money(order.refund_amount),
        },
        "courier_shipment_id": order.courier_shipment_id,
        "delivered_at": _iso(order.delivered_at),
        "return_received_at": _iso(order.return_received_at),
        "lines": lines,
    }
    if order.status == "refunded":
        payload["returned"] = _returned(order, lines)
    return _row("order.status", order.order_id, payload)


def _payment_event(session: Session, attempt: PaymentAttempt) -> FinanceOutbox:
    branch_id = session.connection().execute(
        select(Order.branch_id).where(Order.order_id == attempt.order_id)
    ).scalar_one_or_none()
    return _row("payment.received", attempt.order_id, {
        "attempt_id": str(attempt.attempt_id),
        "order_id": str(attempt.order_id),
        "branch_id": str(branch_id) if branch_id else None,
        "reference": attempt.reference,
        "gateway": attempt.gateway,
        "gateway_ref": attempt.gateway_ref,
        "method_code": attempt.method_code,
        "amount": _money(attempt.amount),
    })


def _commission_event(session: Session, row: ReferralCommission, new: bool) -> FinanceOutbox:
    branch_id = session.connection().execute(
        select(Order.branch_id).where(Order.order_id == row.order_id)
    ).scalar_one_or_none()
    old_status = None if new else (_old(row, "status") or row.status)
    old_amount = None if new else (_old(row, "amount") if _changed(row, "amount") else row.amount)
    return _row("commission.changed", row.order_id, {
        "commission_id": str(row.commission_id),
        "order_id": str(row.order_id) if row.order_id else None,
        "order_number": row.order_number,
        "branch_id": str(branch_id) if branch_id else None,
        "beneficiary": "partner" if row.beneficiary_partner_id else "customer",
        "beneficiary_id": str(row.beneficiary_partner_id or row.beneficiary_user_id),
        "from_status": old_status,
        "to_status": row.status,
        "from_amount": _money(old_amount) if old_amount is not None else None,
        "to_amount": _money(row.amount),
    })


def _before_flush(session: Session, flush_context, instances) -> None:
    if not enabled():
        return
    staged: list[FinanceOutbox] = []
    try:
        for obj in list(session.new):
            # Column defaults apply at INSERT; the event needs the key now.
            if isinstance(obj, Order):
                obj.order_id = obj.order_id or uuid.uuid4()
                staged.append(_order_event(session, obj, None))
            elif isinstance(obj, PaymentAttempt) and obj.status == PaymentAttemptStatus.PAID.value:
                obj.attempt_id = obj.attempt_id or uuid.uuid4()
                staged.append(_payment_event(session, obj))
            elif isinstance(obj, ReferralCommission):
                obj.commission_id = obj.commission_id or uuid.uuid4()
                staged.append(_commission_event(session, obj, new=True))
        for obj in list(session.dirty):
            if isinstance(obj, Order) and _changed(obj, "status"):
                old = _old(obj, "status")
                if old != obj.status:
                    staged.append(_order_event(session, obj, old))
            elif (isinstance(obj, PaymentAttempt) and _changed(obj, "status")
                  and obj.status == PaymentAttemptStatus.PAID.value
                  and _old(obj, "status") != PaymentAttemptStatus.PAID.value):
                staged.append(_payment_event(session, obj))
            elif isinstance(obj, ReferralCommission) and _changed(obj, "status", "amount"):
                staged.append(_commission_event(session, obj, new=False))
    except Exception as exc:  # noqa: BLE001 -- the business write must go ahead
        logger.error(f"finance_outbox: events not recorded for this flush: {exc}")
        return
    for row in staged:
        session.add(row)


def install() -> None:
    """Hook every ORM session in the process. Idempotent."""
    if not event.contains(Session, "before_flush", _before_flush):
        event.listen(Session, "before_flush", _before_flush)
