"""
SRIBEESonline — cancellation review (admin).

Cancelling is a button in the customer's app now, and it moves money: anything
paid by card comes straight back to the Sribees Wallet. A button that moves
money is a button people will test, so this is where a Branch Manager, a Super
Admin or Customer Support goes to see the pattern rather than the individual
event.

Read-only. Nothing here cancels, refunds, blocks or bans — deciding what to do
about a customer is a human judgement with a conversation attached, and a
screen that could act on a heuristic would eventually act on a wrong one. It
answers "who, how often, how fast, how much, and from where", and stops.

Scoped by ``orders:read`` like the rest of the admin order surface, and a
Branch Manager sees their own branch only, through the same branch filter the
order list uses.
"""
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.database import get_db
from app.core.dependencies import BranchScope, inject_branch_filter, require_permission
from app.models.order import Order, OrderCancellation
from app.models.user import Address, User

router = APIRouter(
    dependencies=[Depends(require_permission("orders", "read"))],
    tags=["Admin Cancellations"],
)


def _seconds_to_cancel(row) -> Optional[int]:
    """How long after placing the order it was cancelled, in seconds."""
    if not row.order_placed_at or not row.cancelled_at:
        return None
    placed = row.order_placed_at
    cancelled = row.cancelled_at
    if placed.tzinfo is None:
        placed = placed.replace(tzinfo=timezone.utc)
    if cancelled.tzinfo is None:
        cancelled = cancelled.replace(tzinfo=timezone.utc)
    return int((cancelled - placed).total_seconds())


@router.get("/cancellations", response_model=dict)
async def list_cancellations(
    db: AsyncSession = Depends(get_db),
    scope: BranchScope = Depends(inject_branch_filter),
    days: int = Query(30, ge=1, le=365, description="How far back to look"),
    flagged_only: bool = Query(False),
    user_id: Optional[str] = Query(None, description="One customer's history"),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    """
    Cancellations, newest first, each with the context to judge it.

    Only customer-initiated rows count towards a customer's totals. An admin
    cancelling on someone's behalf is a normal operation, and counting it
    against the customer would turn helping them into evidence against them.
    """
    since = datetime.now(timezone.utc) - timedelta(days=days)

    q = (
        select(
            OrderCancellation,
            Order.order_number,
            Order.total_amount,
            Order.payment_method,
            Order.branch_id,
            User.full_name,
            User.phone,
            User.nic,
        )
        .join(Order, Order.order_id == OrderCancellation.order_id)
        .outerjoin(User, User.user_id == OrderCancellation.user_id)
        .where(OrderCancellation.cancelled_at >= since)
        .order_by(OrderCancellation.cancelled_at.desc())
    )

    # A Branch Manager sees their own branch. Same filter the order list uses,
    # so the two screens can never disagree about what belongs to whom.
    if scope.branch_id:
        q = q.where(Order.branch_id == scope.branch_id)
    if flagged_only:
        q = q.where(OrderCancellation.flagged.is_(True))
    if user_id:
        q = q.where(OrderCancellation.user_id == user_id)

    rows = (await db.execute(q.limit(limit).offset(offset))).all()

    items = []
    for c, order_number, total, method, _branch, name, phone, nic in rows:
        items.append(
            {
                "cancellation_id": str(c.cancellation_id),
                "order_id": str(c.order_id),
                "order_number": order_number,
                "user_id": str(c.user_id) if c.user_id else None,
                "customer_name": name,
                "customer_phone": phone,
                "customer_nic": nic,
                "cancelled_by": c.cancelled_by,
                "reason": c.reason,
                "order_total": float(total or 0),
                "payment_method": method,
                "refund_amount": float(c.refund_amount or 0),
                "courier_fee": float(c.courier_fee or 0),
                # The expensive kind: cancelled AFTER we booked a parcel with
                # SribeesExpress. Worth separating on the screen -- a customer
                # who repeatedly cancels before booking costs a picker's time,
                # one who repeatedly cancels after it costs a courier booking
                # every time.
                "cancelled_after_booking": float(c.courier_fee or 0) > 0,
                "refund_destination": c.refund_destination,
                "ip_address": c.ip_address,
                "device_id": c.device_id,
                "user_agent": c.user_agent,
                "client_platform": c.client_platform,
                "order_placed_at": c.order_placed_at.isoformat() if c.order_placed_at else None,
                "cancelled_at": c.cancelled_at.isoformat() if c.cancelled_at else None,
                "seconds_to_cancel": _seconds_to_cancel(c),
                "flagged": c.flagged,
                "flag_reason": c.flag_reason,
            }
        )

    return {"success": True, "data": {"items": items, "count": len(items)}}


@router.get("/cancellations/customers", response_model=dict)
async def cancellation_leaderboard(
    db: AsyncSession = Depends(get_db),
    scope: BranchScope = Depends(inject_branch_filter),
    days: int = Query(30, ge=1, le=365),
    min_count: int = Query(2, ge=1, description="Hide customers below this many"),
    limit: int = Query(50, ge=1, le=200),
):
    """
    Who cancels, ranked by how much it has cost.

    Ordered by money rather than by count on purpose: ten COD cancellations
    cost us nothing but a picker's time, while two refunded card orders is
    real money leaving. The count is right there to read alongside it.

    ``min_count`` defaults to 2 because one cancellation is a customer
    changing their mind, and a list of those is noise that hides the pattern.
    """
    since = datetime.now(timezone.utc) - timedelta(days=days)

    q = (
        select(
            OrderCancellation.user_id,
            User.full_name,
            User.phone,
            User.nic,
            func.count().label("cancellations"),
            func.coalesce(func.sum(OrderCancellation.refund_amount), 0).label("refunded"),
            func.coalesce(func.sum(OrderCancellation.courier_fee), 0).label("courier_fees"),
            # Cancellations that cost us a courier booking, which is the
            # expensive habit and the one worth spotting early.
            func.sum(
                case((OrderCancellation.courier_fee > 0, 1), else_=0)
            ).label("after_booking"),
            func.sum(case((OrderCancellation.flagged.is_(True), 1), else_=0)).label("flagged"),
            func.min(OrderCancellation.cancelled_at).label("first_seen"),
            func.max(OrderCancellation.cancelled_at).label("last_seen"),
            func.count(func.distinct(OrderCancellation.ip_address)).label("distinct_ips"),
            func.count(func.distinct(OrderCancellation.device_id)).label("distinct_devices"),
        )
        .join(Order, Order.order_id == OrderCancellation.order_id)
        .outerjoin(User, User.user_id == OrderCancellation.user_id)
        .where(
            OrderCancellation.cancelled_at >= since,
            # Customer-initiated only. An admin cancelling for somebody is
            # help, not evidence.
            OrderCancellation.cancelled_by == "customer",
            OrderCancellation.user_id.isnot(None),
        )
        .group_by(OrderCancellation.user_id, User.full_name, User.phone, User.nic)
        .having(func.count() >= min_count)
        .order_by(func.coalesce(func.sum(OrderCancellation.refund_amount), 0).desc())
        .limit(limit)
    )
    if scope.branch_id:
        q = q.where(Order.branch_id == scope.branch_id)

    rows = (await db.execute(q)).all()

    return {
        "success": True,
        "data": {
            "items": [
                {
                    "user_id": str(r.user_id),
                    "customer_name": r.full_name,
                    "customer_phone": r.phone,
                    "customer_nic": r.nic,
                    "cancellations": r.cancellations,
                    "refunded_total": float(r.refunded or 0),
                    "courier_fees_charged": float(r.courier_fees or 0),
                    "cancelled_after_booking": int(r.after_booking or 0),
                    "flagged": int(r.flagged or 0),
                    "distinct_ips": r.distinct_ips,
                    "distinct_devices": r.distinct_devices,
                    "first_seen": r.first_seen.isoformat() if r.first_seen else None,
                    "last_seen": r.last_seen.isoformat() if r.last_seen else None,
                }
                for r in rows
            ],
            "days": days,
        },
    }


@router.get("/cancellations/customers/{user_id}/links", response_model=dict)
async def linked_accounts(
    user_id: str,
    db: AsyncSession = Depends(get_db),
):
    """
    Other accounts that look like the same person.

    The NIC cannot be the answer here, however natural it feels: it is UNIQUE
    (``ix_users_nic_unique``), so two accounts can never share one. What a
    person CAN do is open several accounts with different NICs and cancel from
    each — and what gives that away is the things they did not think to vary.

    Three signals, in descending order of how hard they are to fake:

      * **device** — the same phone. Strongest of the three, and the only one
        that survives a new number and a new name.
      * **address** — the same doorstep. Goods have to arrive somewhere.
      * **IP** — the same connection. Weakest: a household, an office or a
        mobile carrier's NAT shares one legitimately, so it is a prompt to
        look and never a conclusion.

    A match here means "worth a look", never "guilty". Anyone reading this
    screen should treat it as a question, not an answer.
    """
    device_ids = (
        await db.execute(
            select(func.distinct(OrderCancellation.device_id)).where(
                OrderCancellation.user_id == user_id,
                OrderCancellation.device_id.isnot(None),
            )
        )
    ).scalars().all()

    ips = (
        await db.execute(
            select(func.distinct(OrderCancellation.ip_address)).where(
                OrderCancellation.user_id == user_id,
                OrderCancellation.ip_address.isnot(None),
            )
        )
    ).scalars().all()

    by_device = []
    if device_ids:
        rows = (
            await db.execute(
                select(
                    OrderCancellation.user_id,
                    User.full_name,
                    User.phone,
                    User.nic,
                    func.count().label("cancellations"),
                )
                .outerjoin(User, User.user_id == OrderCancellation.user_id)
                .where(
                    OrderCancellation.device_id.in_(device_ids),
                    OrderCancellation.user_id != user_id,
                    OrderCancellation.user_id.isnot(None),
                )
                .group_by(OrderCancellation.user_id, User.full_name, User.phone, User.nic)
            )
        ).all()
        by_device = [
            {
                "user_id": str(r.user_id),
                "customer_name": r.full_name,
                "customer_phone": r.phone,
                "customer_nic": r.nic,
                "cancellations": r.cancellations,
            }
            for r in rows
        ]

    by_ip = []
    if ips:
        rows = (
            await db.execute(
                select(
                    OrderCancellation.user_id,
                    User.full_name,
                    func.count().label("cancellations"),
                )
                .outerjoin(User, User.user_id == OrderCancellation.user_id)
                .where(
                    OrderCancellation.ip_address.in_(ips),
                    OrderCancellation.user_id != user_id,
                    OrderCancellation.user_id.isnot(None),
                )
                .group_by(OrderCancellation.user_id, User.full_name)
            )
        ).all()
        by_ip = [
            {
                "user_id": str(r.user_id),
                "customer_name": r.full_name,
                "cancellations": r.cancellations,
            }
            for r in rows
        ]

    # Same doorstep. Normalised loosely -- lower-cased line 1 plus city -- so a
    # difference of capitals or spacing does not hide a match.
    addr_keys = (
        await db.execute(
            select(
                func.lower(func.trim(Address.address_line1)),
                func.lower(func.trim(func.coalesce(Address.postal_city, ""))),
            ).where(Address.user_id == user_id)
        )
    ).all()

    by_address = []
    if addr_keys:
        conditions = [
            (
                func.lower(func.trim(Address.address_line1)) == line1,
                func.lower(func.trim(func.coalesce(Address.postal_city, ""))) == city,
            )
            for line1, city in addr_keys
        ]
        from sqlalchemy import and_, or_

        rows = (
            await db.execute(
                select(
                    Address.user_id,
                    User.full_name,
                    User.phone,
                )
                .outerjoin(User, User.user_id == Address.user_id)
                .where(
                    or_(*[and_(*c) for c in conditions]),
                    Address.user_id != user_id,
                )
                .group_by(Address.user_id, User.full_name, User.phone)
            )
        ).all()
        by_address = [
            {
                "user_id": str(r.user_id),
                "customer_name": r.full_name,
                "customer_phone": r.phone,
            }
            for r in rows
        ]

    return {
        "success": True,
        "data": {
            "by_device": by_device,
            "by_ip": by_ip,
            "by_address": by_address,
            "note": (
                "A shared IP is normal for a household, an office or a mobile "
                "carrier. Treat every match as a question, not an answer."
            ),
        },
    }
