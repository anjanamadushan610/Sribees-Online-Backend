"""
Sales Velocity Service

How fast each product is actually moving out of one branch, over a rolling
window. Backs the Branch Product Catalog's "Sales (30 days)" column and its
Fast-Moving / Slow-Moving filters.

The point of the feature is not the number, it is the decision it drives: a
Marketing Manager looking at this page is deciding **what to put on Quick
Sale**. Slow-moving stock that is also perishable is money about to be thrown
away, and until now nothing in the admin told them which items those were.

Only DELIVERED orders count. A pending or cancelled order has not moved
anything off a shelf, and counting it would make dead stock look like it is
selling — the exact error that leaves a manager doing nothing while it spoils.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.order import Order, OrderItem, OrderStatus

# The reporting window. A month smooths out the weekly shopping rhythm without
# being so long that a product's recent collapse is hidden by its good June.
VELOCITY_WINDOW_DAYS = 30

# Units sold in the window that separate the three bands. Expressed per 30 days
# and rescaled for other windows by `thresholds_for`, so a 7-day view does not
# label everything "slow" simply because the window is shorter.
FAST_MOVING_UNITS = 100
STEADY_MOVING_UNITS = 30


def thresholds_for(window_days: int) -> tuple[int, int]:
    """
    ``(fast, steady)`` unit thresholds scaled to an arbitrary window.

    Without this a 7-day view would compare a week's sales against a month's
    bar and report almost every product as Slow-Moving — which would send a
    manager clearancing healthy stock.
    """
    factor = max(window_days, 1) / VELOCITY_WINDOW_DAYS
    return (
        max(1, round(FAST_MOVING_UNITS * factor)),
        max(1, round(STEADY_MOVING_UNITS * factor)),
    )


def classify(units: int, window_days: int = VELOCITY_WINDOW_DAYS) -> str:
    """``'fast' | 'steady' | 'slow' | 'none'`` for a unit count."""
    fast, steady = thresholds_for(window_days)
    if units >= fast:
        return "fast"
    if units >= steady:
        return "steady"
    if units > 0:
        return "slow"
    return "none"


async def units_sold(
    db: AsyncSession,
    branch_id: UUID,
    product_ids: Optional[Iterable[UUID]] = None,
    window_days: int = VELOCITY_WINDOW_DAYS,
) -> dict[UUID, int]:
    """
    Units of each product delivered from this branch inside the window.

    One grouped query for the whole page. Products with no sales are simply
    absent from the result — callers treat absence as zero, which is correct
    and avoids emitting a row per product in the catalog.

    Joins ``orders`` rather than filtering ``order_items`` alone because the
    status and the branch both live on the parent order; matching on
    ``order_items`` by itself would count cancelled baskets as sales.
    """
    since = datetime.now(timezone.utc) - timedelta(days=window_days)

    stmt = (
        select(
            OrderItem.product_id.label("product_id"),
            func.coalesce(func.sum(OrderItem.quantity), 0).label("units"),
        )
        .join(Order, Order.order_id == OrderItem.order_id)
        .where(
            Order.branch_id == branch_id,
            Order.status == OrderStatus.DELIVERED.value,
            Order.created_at >= since,
        )
        .group_by(OrderItem.product_id)
    )

    ids = list(product_ids) if product_ids is not None else None
    if ids is not None:
        if not ids:
            return {}
        stmt = stmt.where(OrderItem.product_id.in_(ids))

    rows = (await db.execute(stmt)).all()
    return {r.product_id: int(r.units or 0) for r in rows}


def stock_state(quantity: int, low_threshold: int = 10) -> str:
    """
    ``'out' | 'low' | 'in'``.

    ``low_threshold`` defaults to 10 to match the catalog's own
    ``low_stock_threshold`` default, so the badge on this page and the low-stock
    alerts elsewhere draw the line in the same place.
    """
    if quantity <= 0:
        return "out"
    if quantity < low_threshold:
        return "low"
    return "in"
