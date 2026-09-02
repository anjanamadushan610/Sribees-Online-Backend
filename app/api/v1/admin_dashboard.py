"""
Admin Dashboard API

The generic "staff" landing page's stats — separate from admin_analytics.py
(gated `require_roles("super_admin", "branch_manager")` at the router level,
which can't be selectively relaxed for one route). This one is gated purely
on the `dashboard:read` delegatable permission, and returns each metric only
if the caller also holds the specific permission that metric represents —
`dashboard:read` alone unlocks the page, not every number on it.

Reuses admin_analytics.py's revenue/window helpers by import rather than
redefining them, so this page and the Analytics report can never disagree
about what counts as revenue or how a period is bucketed.

Prefix "/admin/dashboard" is applied by app/api/v1/router.py.
"""
from datetime import timedelta

from fastapi import APIRouter, Depends
from sqlalchemy import distinct, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.v1.admin_analytics import (
    Window,
    _in_window,
    _pct_change,
    _period_totals,
    _revenue_sum,
    _scoped,
)
from app.config.database import get_db
from app.core.dependencies import BranchScope, inject_branch_filter, require_permission
from app.models.admin import Admin
from app.models.order import Order, OrderItem, OrderStatus
from app.services.permission_service import PermissionService

router = APIRouter(tags=["Admin Dashboard"])

# "Still waiting on someone" — every status except the three terminal ones.
# Must stay in sync with the identical set in
# admin/src/pages/Dashboard/DashboardHome.tsx (OPEN_STATUSES).
_OPEN_STATUSES = {
    OrderStatus.PENDING.value,
    OrderStatus.CONFIRMED.value,
    OrderStatus.PROCESSING.value,
    OrderStatus.PACKING.value,
    OrderStatus.PACKED.value,
    OrderStatus.HANDED_TO_COURIER.value,
    OrderStatus.SHIPPED.value,
    OrderStatus.OUT_FOR_DELIVERY.value,
    OrderStatus.DELIVERY_FAILED.value,
    OrderStatus.RETURN_REQUESTED.value,
    OrderStatus.RETURN_APPROVED.value,
}


@router.get("/stats", response_model=dict)
async def dashboard_stats(
    db: AsyncSession = Depends(get_db),
    admin: Admin = Depends(require_permission("dashboard", "read")),
    scope: BranchScope = Depends(inject_branch_filter),
):
    """
    Stats for the generic dashboard (today: only the 'staff' role lands
    here — every base role has its own dedicated dashboard). Each section
    is included only if the caller holds the permission it represents.
    """
    permissions = await PermissionService.get_effective_permissions(db, admin)
    can_orders = ("orders", "read") in permissions
    can_customers = ("customers", "read") in permissions
    can_products = ("products", "read") in permissions
    # Revenue / financial figures are gated on analytics:read, NOT orders:read
    # (QA spec B6): an order-support account legitimately holds orders:read to
    # do its job and must never see business revenue as a side effect.
    can_revenue = ("analytics", "read") in permissions
    effective_branch = scope.resolve(None)

    data: dict = {
        "granted": {
            "orders": can_orders,
            "customers": can_customers,
            "products": can_products,
            "revenue": can_revenue,
        }
    }

    if can_orders:
        lifetime = (
            await db.execute(
                _scoped(
                    select(
                        _revenue_sum().label("revenue"),
                        func.count(Order.order_id).label("orders"),
                    ),
                    effective_branch,
                )
            )
        ).one()

        pending_orders = (
            await db.execute(
                _scoped(
                    select(func.count(Order.order_id)).where(
                        Order.status.in_(_OPEN_STATUSES)
                    ),
                    effective_branch,
                )
            )
        ).scalar() or 0

        window = Window(7)
        current = await _period_totals(db, effective_branch, window.bounds)
        previous = await _period_totals(db, effective_branch, window.prev_bounds)

        # 7-day daily revenue/order series, zero-filled — same bucketing as
        # admin_analytics.py's analytics_sales, kept local since it's the
        # only place in this endpoint that needs it.
        order_date = func.date(func.timezone("UTC", Order.created_at)).label("day")
        series_stmt = (
            _in_window(
                _scoped(
                    select(
                        order_date,
                        func.count(Order.order_id).label("orders"),
                        _revenue_sum().label("revenue"),
                    ),
                    effective_branch,
                ),
                window.bounds,
            )
            .group_by(order_date)
            .order_by(order_date)
        )
        by_day = {
            (day.isoformat() if hasattr(day, "isoformat") else str(day)): {
                "orders": int(orders or 0),
                "revenue": float(revenue or 0),
            }
            for day, orders, revenue in (await db.execute(series_stmt)).all()
        }
        revenue_trend = []
        for i in range(7):
            d = (window.start + timedelta(days=i)).isoformat()
            entry = by_day.get(d, {"orders": 0, "revenue": 0.0})
            revenue_trend.append({"date": d, **entry})

        status_stmt = _in_window(
            _scoped(
                select(Order.status, func.count(Order.order_id).label("orders")),
                effective_branch,
            ),
            window.bounds,
        ).group_by(Order.status)
        found_status = {
            row.status: int(row.orders or 0) for row in (await db.execute(status_stmt)).all()
        }
        order_status_distribution = [
            {"status": s.value, "orders": found_status.get(s.value, 0)} for s in OrderStatus
        ]

        recent = (
            await db.execute(
                _scoped(
                    select(Order).options(selectinload(Order.user)),
                    effective_branch,
                )
                .order_by(Order.created_at.desc())
                .limit(5)
            )
        ).scalars().all()
        recent_orders = [
            {
                "order_id": str(o.order_id),
                "order_number": o.order_number,
                "customer_name": o.user.full_name if o.user else "Unknown customer",
                "total_amount": float(o.total_amount),
                "status": o.status,
                "created_at": o.created_at.isoformat() if o.created_at else None,
            }
            for o in recent
        ]

        # Operational figures — visible to anyone with orders:read.
        data.update(
            {
                "total_orders": int(lifetime.orders or 0),
                "pending_orders": int(pending_orders),
                "orders_growth": _pct_change(current["orders"], previous["orders"]) or 0,
                "order_status_distribution": order_status_distribution,
                "recent_orders": recent_orders,
                # Order volume per day (no money) — the operational trend.
                "orders_trend": [
                    {"date": e["date"], "orders": e["orders"]} for e in revenue_trend
                ],
            }
        )

        # Financial figures — analytics:read only (B6).
        if can_revenue:
            data.update(
                {
                    "total_revenue": float(lifetime.revenue or 0),
                    "revenue_growth": _pct_change(current["revenue"], previous["revenue"]) or 0,
                    "revenue_trend": revenue_trend,
                }
            )

    if can_customers:
        total_customers = (
            await db.execute(
                _scoped(select(func.count(distinct(Order.user_id))), effective_branch)
            )
        ).scalar() or 0
        data["total_customers"] = int(total_customers)

    if can_orders and can_products:
        window30 = Window(30)
        top_stmt = (
            _in_window(
                _scoped(
                    select(
                        OrderItem.product_id,
                        func.max(OrderItem.product_name).label("name"),
                        func.coalesce(func.sum(OrderItem.quantity), 0).label("units"),
                        func.coalesce(func.sum(OrderItem.subtotal), 0).label("revenue"),
                    )
                    .select_from(OrderItem)
                    .join(Order, Order.order_id == OrderItem.order_id),
                    effective_branch,
                ),
                window30.bounds,
            )
            .group_by(OrderItem.product_id)
            .order_by(func.coalesce(func.sum(OrderItem.subtotal), 0).desc())
            .limit(5)
        )
        data["top_selling_products"] = [
            {
                "product_id": str(r.product_id),
                "name": r.name,
                "units_sold": int(r.units or 0),
                # Per-product revenue is a financial figure — analytics:read only.
                **({"revenue": float(r.revenue or 0)} if can_revenue else {}),
            }
            for r in (await db.execute(top_stmt)).all()
        ]

    return {"success": True, "data": data}
