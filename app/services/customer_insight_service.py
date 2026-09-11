"""
Customer Insight Service

Lifetime value and behavioural segmentation for the branch Customer Directory.

Exists as its own module because two endpoints (the directory list and the
profile drawer) must agree to the rupee on what a customer has spent, and they
previously did not: the list showed nothing and the profile summed every order
that was not literally ``cancelled``, so a refunded order still counted as
revenue from that customer. A customer whose only order was refunded read as a
paying customer, and any retention decision taken off that number was taken off
a fiction.

The rule this module enforces everywhere:

    Net Lifetime Spend = SUM(order.total_amount) WHERE status = 'delivered'

Nothing else counts. Not pending (not yet money), not shipped (not yet
delivered), and emphatically not refunded or returned (money given back).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional
from uuid import UUID

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.order import Order, OrderStatus

# A customer with no completed order in this long has stopped buying from us.
# Grocery is a weekly-to-fortnightly rhythm, so a month of silence is a real
# signal rather than noise.
AT_RISK_DAYS = 30

# Two or more completed orders is the line between "tried us" and "shops here".
RETURNING_MIN_ORDERS = 2

# Money actually received and kept.
_EARNED = (OrderStatus.DELIVERED.value,)

# Money handed back. Tracked separately so the UI can say *why* a customer's
# net spend is lower than their order history suggests, instead of leaving a
# manager to wonder whether the number is broken.
_REFUNDED = (OrderStatus.REFUNDED.value,)


class CustomerStats:
    """One customer's purchasing history, already branch-scoped."""

    __slots__ = (
        "user_id",
        "completed_orders",
        "net_spent",
        "refunded_amount",
        "last_order_at",
    )

    def __init__(
        self,
        user_id: UUID,
        completed_orders: int = 0,
        net_spent: float = 0.0,
        refunded_amount: float = 0.0,
        last_order_at: Optional[datetime] = None,
    ):
        self.user_id = user_id
        self.completed_orders = completed_orders
        self.net_spent = net_spent
        self.refunded_amount = refunded_amount
        self.last_order_at = last_order_at

    @property
    def has_refund(self) -> bool:
        return self.refunded_amount > 0

    def segment(self, joined_at: Optional[datetime] = None, now: Optional[datetime] = None) -> str:
        """
        ``'returning' | 'new' | 'at_risk'``.

        Precedence is deliberate: **at_risk wins over returning.** A customer
        with seven orders who has bought nothing for two months is the single
        most valuable person to win back, and calling them "Returning" because
        of history buries them in the one list a manager scrolls past. What
        matters is recency, then depth.

        A customer who has never completed an order is only "at risk" once
        they have been registered long enough to have plausibly bought
        something; before that they are simply new.
        """
        now = now or datetime.now(timezone.utc)
        cutoff = now - timedelta(days=AT_RISK_DAYS)

        if self.completed_orders == 0:
            if joined_at is not None and _aware(joined_at) < cutoff:
                return "at_risk"
            return "new"

        if self.last_order_at is not None and _aware(self.last_order_at) < cutoff:
            return "at_risk"

        if self.completed_orders >= RETURNING_MIN_ORDERS:
            return "returning"
        return "new"

    def as_dict(self, joined_at: Optional[datetime] = None) -> dict[str, Any]:
        return {
            "completed_orders": self.completed_orders,
            "net_spent": round(self.net_spent, 2),
            "refunded_amount": round(self.refunded_amount, 2),
            "has_refund": self.has_refund,
            "last_order_at": (
                self.last_order_at.isoformat() if self.last_order_at else None
            ),
            "segment": self.segment(joined_at),
        }


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _stats_select(branch_id: Optional[UUID]):
    """The one aggregate both callers share, so they cannot disagree."""
    filters = []
    if branch_id is not None:
        filters.append(Order.branch_id == branch_id)

    return (
        select(
            Order.user_id.label("user_id"),
            func.count(
                case((Order.status.in_(_EARNED), Order.order_id), else_=None)
            ).label("completed_orders"),
            func.coalesce(
                func.sum(
                    case((Order.status.in_(_EARNED), Order.total_amount), else_=0)
                ),
                0,
            ).label("net_spent"),
            func.coalesce(
                func.sum(
                    case((Order.status.in_(_REFUNDED), Order.total_amount), else_=0)
                ),
                0,
            ).label("refunded_amount"),
            func.max(
                case((Order.status.in_(_EARNED), Order.created_at), else_=None)
            ).label("last_order_at"),
        )
        .where(*filters)
        .group_by(Order.user_id)
    )


async def stats_for_users(
    db: AsyncSession, user_ids: Iterable[UUID], branch_id: Optional[UUID] = None
) -> dict[UUID, CustomerStats]:
    """
    Purchasing stats for a page of customers, in ONE grouped query.

    Batched because the directory renders 20+ rows and a per-row query would
    make the page's cost scale with its length. A customer with no orders in
    scope is simply absent from the result; callers substitute a zeroed
    ``CustomerStats`` rather than treating absence as an error.
    """
    ids = list(user_ids)
    if not ids:
        return {}

    rows = (
        await db.execute(_stats_select(branch_id).where(Order.user_id.in_(ids)))
    ).all()
    return {
        r.user_id: CustomerStats(
            user_id=r.user_id,
            completed_orders=int(r.completed_orders or 0),
            net_spent=float(r.net_spent or 0),
            refunded_amount=float(r.refunded_amount or 0),
            last_order_at=r.last_order_at,
        )
        for r in rows
    }


async def stats_for_user(
    db: AsyncSession, user_id: UUID, branch_id: Optional[UUID] = None
) -> CustomerStats:
    """Same figures for one customer. Zeroed when they have no orders in scope."""
    result = await stats_for_users(db, [user_id], branch_id)
    return result.get(user_id) or CustomerStats(user_id=user_id)
