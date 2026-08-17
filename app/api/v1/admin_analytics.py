"""
Admin Analytics API (Module 7.1)

Branch-scoped dashboard + reporting metrics. Restricted to super_admin and
branch_manager (enforced at the router level). Branch isolation runs through
``inject_branch_filter``: a Branch Manager only ever sees their own branch's
figures, while a Super Admin sees the whole network and may narrow to one
branch with ``?branch_id=``.

Prefix "/admin/analytics" is applied by app/api/v1/router.py.

Revenue definition (used identically by every endpoint here, so the Dashboard
and the Analytics report can never disagree):

    an order counts toward revenue when payment_status = 'paid'
    AND status NOT IN ('cancelled', 'refunded')

i.e. money actually collected and not given back. Order *counts* are separate
and include every status, because "how many orders came in" is a demand
question, not a cash question.
"""
from datetime import date, datetime, time, timedelta, timezone
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy import and_, case, distinct, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.database import get_db
from app.core.dependencies import BranchScope, inject_branch_filter, require_roles
from app.models.branch import Branch
from app.models.category import Category
from app.models.order import Order, OrderItem, OrderStatus, PaymentStatus
from app.models.product import BranchInventory, Product
from app.models.user import User

router = APIRouter(
    dependencies=[Depends(require_roles("super_admin", "branch_manager"))],
    tags=["Admin Analytics"],
)

_PAID = PaymentStatus.PAID.value
_VOID_STATUSES = (OrderStatus.CANCELLED.value, OrderStatus.REFUNDED.value)

# Shared query params, declared once so every endpoint documents them the same.
DaysQuery = Query(30, ge=1, le=365, description="Window size in days (inclusive of today)")
BranchQuery = Query(
    None, description="Super Admin only: scope to a branch (ignored for Branch Managers)"
)
LimitQuery = Query(10, ge=1, le=50, description="Maximum rows to return")


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

class Window:
    """
    A closed [start, end] day range plus the equal-length window before it.

    Days are UTC calendar days, and every query in this module keys off these
    same instants. Consistency is what matters more than the choice of zone: a
    KPI card and the chart under it must never be measuring different periods.
    """

    def __init__(self, days: int):
        today = datetime.now(timezone.utc).date()
        self.days = days
        self.end: date = today
        self.start: date = today - timedelta(days=days - 1)
        self.prev_end: date = self.start - timedelta(days=1)
        self.prev_start: date = self.prev_end - timedelta(days=days - 1)

    @staticmethod
    def _floor(d: date) -> datetime:
        return datetime.combine(d, time.min, tzinfo=timezone.utc)

    @staticmethod
    def _ceil(d: date) -> datetime:
        return datetime.combine(d + timedelta(days=1), time.min, tzinfo=timezone.utc)

    @property
    def bounds(self) -> tuple[datetime, datetime]:
        return self._floor(self.start), self._ceil(self.end)

    @property
    def prev_bounds(self) -> tuple[datetime, datetime]:
        return self._floor(self.prev_start), self._ceil(self.prev_end)

    def as_dict(self) -> dict:
        return {
            "days": self.days,
            "start_date": self.start.isoformat(),
            "end_date": self.end.isoformat(),
            "previous_start_date": self.prev_start.isoformat(),
            "previous_end_date": self.prev_end.isoformat(),
        }


def _is_revenue():
    """SQL predicate: this order's money counts."""
    return and_(Order.payment_status == _PAID, Order.status.notin_(_VOID_STATUSES))


def _revenue_sum(column=Order.total_amount):
    """SUM of ``column`` over revenue-eligible orders only, never NULL."""
    return func.coalesce(func.sum(case((_is_revenue(), column), else_=0)), 0)


def _scoped(stmt, branch_id: Optional[UUID]):
    """Apply the resolved branch filter to a statement selecting from orders."""
    return stmt.where(Order.branch_id == branch_id) if branch_id else stmt


def _in_window(stmt, bounds: tuple[datetime, datetime]):
    start, end = bounds
    return stmt.where(Order.created_at >= start, Order.created_at < end)


def _pct_change(current: float, previous: float) -> Optional[float]:
    """
    Percentage change, or None when there is no baseline to compare against.

    None is deliberate: a jump from 0 to 5 is not "+500%", it is "new", and the
    UI should say so rather than print a meaningless number.
    """
    if previous == 0:
        return None
    return round(((current - previous) / previous) * 100, 1)


def _scope_dict(scope: BranchScope, effective_branch: Optional[UUID]) -> dict:
    return {
        "is_super_admin": scope.is_super_admin,
        "branch_id": str(effective_branch) if effective_branch else None,
    }


async def _period_totals(
    db: AsyncSession, branch_id: Optional[UUID], bounds: tuple[datetime, datetime]
) -> dict:
    """Revenue / orders / customers / AOV / items for one time window."""
    stmt = _in_window(
        _scoped(
            select(
                _revenue_sum().label("revenue"),
                func.count(Order.order_id).label("orders"),
                func.count(distinct(Order.user_id)).label("customers"),
                func.coalesce(
                    func.sum(case((_is_revenue(), 1), else_=0)), 0
                ).label("paid_orders"),
            ),
            branch_id,
        ),
        bounds,
    )
    row = (await db.execute(stmt)).one()

    items_stmt = _in_window(
        _scoped(
            select(func.coalesce(func.sum(OrderItem.quantity), 0))
            .select_from(OrderItem)
            .join(Order, Order.order_id == OrderItem.order_id)
            .where(_is_revenue()),
            branch_id,
        ),
        bounds,
    )
    items_sold = int((await db.execute(items_stmt)).scalar() or 0)

    revenue = float(row.revenue or 0)
    paid_orders = int(row.paid_orders or 0)
    return {
        "revenue": revenue,
        "orders": int(row.orders or 0),
        "paid_orders": paid_orders,
        "customers": int(row.customers or 0),
        "items_sold": items_sold,
        # AOV is revenue per *paying* order — dividing by all orders would drag
        # the figure down every time someone abandons an unpaid order.
        "avg_order_value": round(revenue / paid_orders, 2) if paid_orders else 0.0,
    }


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.get("/summary", response_model=dict)
async def analytics_summary(
    days: int = DaysQuery,
    branch_id: Optional[UUID] = BranchQuery,
    db: AsyncSession = Depends(get_db),
    scope: BranchScope = Depends(inject_branch_filter),
):
    """
    Headline KPIs for the selected window, with the equal-length preceding
    window alongside it so the UI can show a trend arrow.

    ``total_revenue`` / ``total_orders`` / ``active_customers`` /
    ``low_stock_alerts`` are lifetime-to-date figures kept at the top level for
    the Dashboard's KPI cards; ``current`` / ``previous`` / ``deltas`` are the
    windowed view used by the Analytics report.
    """
    effective_branch = scope.resolve(branch_id)
    window = Window(days)

    current = await _period_totals(db, effective_branch, window.bounds)
    previous = await _period_totals(db, effective_branch, window.prev_bounds)

    # Lifetime figures (no date filter) — "total revenue since launch".
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

    # Active customers = distinct buyers inside the selected window.
    active_customers = current["customers"]

    # Low-stock alerts — branch_inventory rows at/below their threshold.
    low_stock_q = select(func.count(BranchInventory.inventory_id)).where(
        BranchInventory.stock_quantity <= BranchInventory.low_stock_threshold
    )
    if effective_branch:
        low_stock_q = low_stock_q.where(BranchInventory.branch_id == effective_branch)
    low_stock_alerts = int((await db.execute(low_stock_q)).scalar() or 0)

    # New customers — first-ever order landed inside this window.
    first_order = (
        select(
            Order.user_id.label("user_id"),
            func.min(Order.created_at).label("first_order_at"),
        )
        .group_by(Order.user_id)
    )
    if effective_branch:
        first_order = first_order.where(Order.branch_id == effective_branch)
    fo = first_order.subquery()
    start, end = window.bounds
    new_customers = int(
        (
            await db.execute(
                select(func.count()).select_from(fo).where(
                    fo.c.first_order_at >= start, fo.c.first_order_at < end
                )
            )
        ).scalar()
        or 0
    )

    return {
        "success": True,
        "data": {
            # Lifetime KPI cards (unchanged contract).
            "total_revenue": float(lifetime.revenue or 0),
            "total_orders": int(lifetime.orders or 0),
            "active_customers": active_customers,
            "low_stock_alerts": low_stock_alerts,
            # Windowed comparison.
            "range": window.as_dict(),
            "current": current,
            "previous": previous,
            "new_customers": new_customers,
            "deltas": {
                "revenue": _pct_change(current["revenue"], previous["revenue"]),
                "orders": _pct_change(current["orders"], previous["orders"]),
                "customers": _pct_change(current["customers"], previous["customers"]),
                "items_sold": _pct_change(current["items_sold"], previous["items_sold"]),
                "avg_order_value": _pct_change(
                    current["avg_order_value"], previous["avg_order_value"]
                ),
            },
            "scope": _scope_dict(scope, effective_branch),
        },
    }


@router.get("/sales", response_model=dict)
async def analytics_sales(
    days: int = DaysQuery,
    branch_id: Optional[UUID] = BranchQuery,
    compare: bool = Query(False, description="Also return the preceding window's series"),
    db: AsyncSession = Depends(get_db),
    scope: BranchScope = Depends(inject_branch_filter),
):
    """
    Daily revenue + order-count time series. Missing days are zero-filled so the
    chart never draws a broken line. With ``compare=true`` the preceding window
    is returned as ``previous_series``, index-aligned day-for-day.
    """
    effective_branch = scope.resolve(branch_id)
    window = Window(days)
    # `window.end` is always today (see Window.__init__), so the current
    # series' last bucket covers a day that has not finished yet — flag it so
    # the chart can draw it as "in progress" instead of a misleading drop.
    today_iso = datetime.now(timezone.utc).date().isoformat()

    async def series_for(start_day: date, bounds: tuple[datetime, datetime]) -> list[dict]:
        # Bucket by the UTC calendar day, explicitly. A bare date(created_at)
        # would bucket in the *database session's* timezone while the window
        # bounds above are UTC instants — so on any server not set to UTC the
        # two disagree at the edges and the orders that fall in the gap are
        # silently dropped from the zero-filled series. The chart then fails to
        # add up to the KPI card sitting right above it.
        order_date = func.date(func.timezone("UTC", Order.created_at)).label("day")
        stmt = _in_window(
            _scoped(
                select(
                    order_date,
                    func.count(Order.order_id).label("orders"),
                    _revenue_sum().label("revenue"),
                ),
                effective_branch,
            ),
            bounds,
        ).group_by(order_date).order_by(order_date)

        by_day: dict[str, dict] = {}
        for day, orders, revenue in (await db.execute(stmt)).all():
            key = day.isoformat() if hasattr(day, "isoformat") else str(day)
            by_day[key] = {"orders": int(orders or 0), "revenue": float(revenue or 0)}

        out = []
        for i in range(days):
            d = (start_day + timedelta(days=i)).isoformat()
            entry = by_day.get(d, {"orders": 0, "revenue": 0.0})
            out.append(
                {
                    "date": d,
                    "orders": entry["orders"],
                    "revenue": entry["revenue"],
                    "is_partial": d == today_iso,
                }
            )
        return out

    data = {
        "series": await series_for(window.start, window.bounds),
        "days": days,
        "range": window.as_dict(),
        "scope": _scope_dict(scope, effective_branch),
    }
    if compare:
        data["previous_series"] = await series_for(window.prev_start, window.prev_bounds)

    return {"success": True, "data": data}


@router.get("/branches", response_model=dict)
async def analytics_branches(
    days: int = DaysQuery,
    db: AsyncSession = Depends(get_db),
    scope: BranchScope = Depends(inject_branch_filter),
):
    """
    Per-branch performance breakdown — the Super Admin's network-wide view.

    Every branch is listed, including ones with no orders in the window (they
    come back as zeros), because a branch that sold nothing is exactly the row
    a Super Admin needs to see. Branch Managers get a single row: their own.

    ``revenue_share`` is each branch's percentage of the total revenue in the
    window, so the rows always describe the same pie the chart draws.
    """
    window = Window(days)
    start, end = window.bounds
    prev_start, prev_end = window.prev_bounds

    branch_q = select(Branch).order_by(Branch.name)
    if not scope.is_super_admin:
        branch_q = branch_q.where(Branch.branch_id == scope.branch_id)
    branches = (await db.execute(branch_q)).scalars().all()

    # One grouped pass per metric family, then stitched together in Python —
    # cheaper and far easier to read than a five-way LEFT JOIN.
    async def totals_by_branch(bounds: tuple[datetime, datetime]) -> dict[UUID, dict]:
        stmt = _in_window(
            select(
                Order.branch_id,
                _revenue_sum().label("revenue"),
                func.count(Order.order_id).label("orders"),
                func.coalesce(func.sum(case((_is_revenue(), 1), else_=0)), 0).label("paid"),
                func.count(distinct(Order.user_id)).label("customers"),
                func.coalesce(
                    func.sum(case((Order.status.in_(_VOID_STATUSES), 1), else_=0)), 0
                ).label("cancelled"),
            ).where(Order.branch_id.isnot(None)),
            bounds,
        ).group_by(Order.branch_id)
        return {
            row.branch_id: {
                "revenue": float(row.revenue or 0),
                "orders": int(row.orders or 0),
                "paid": int(row.paid or 0),
                "customers": int(row.customers or 0),
                "cancelled": int(row.cancelled or 0),
            }
            for row in (await db.execute(stmt)).all()
        }

    current = await totals_by_branch((start, end))
    previous = await totals_by_branch((prev_start, prev_end))

    items_stmt = (
        select(
            Order.branch_id,
            func.coalesce(func.sum(OrderItem.quantity), 0).label("items"),
        )
        .select_from(OrderItem)
        .join(Order, Order.order_id == OrderItem.order_id)
        .where(_is_revenue(), Order.branch_id.isnot(None))
        .where(Order.created_at >= start, Order.created_at < end)
        .group_by(Order.branch_id)
    )
    items_by_branch = {r.branch_id: int(r.items or 0) for r in (await db.execute(items_stmt)).all()}

    stock_stmt = select(
        BranchInventory.branch_id,
        func.count(BranchInventory.inventory_id).label("skus"),
        func.coalesce(
            func.sum(
                case(
                    (
                        BranchInventory.stock_quantity
                        <= BranchInventory.low_stock_threshold,
                        1,
                    ),
                    else_=0,
                )
            ),
            0,
        ).label("low_stock"),
        func.coalesce(func.sum(BranchInventory.stock_quantity), 0).label("units_on_hand"),
    ).group_by(BranchInventory.branch_id)
    stock_by_branch = {
        r.branch_id: {
            "skus": int(r.skus or 0),
            "low_stock": int(r.low_stock or 0),
            "units_on_hand": int(r.units_on_hand or 0),
        }
        for r in (await db.execute(stock_stmt)).all()
    }

    total_revenue = sum(v["revenue"] for v in current.values())

    rows = []
    for b in branches:
        cur = current.get(
            b.branch_id,
            {"revenue": 0.0, "orders": 0, "paid": 0, "customers": 0, "cancelled": 0},
        )
        prv = previous.get(
            b.branch_id,
            {"revenue": 0.0, "orders": 0, "paid": 0, "customers": 0, "cancelled": 0},
        )
        stock = stock_by_branch.get(
            b.branch_id, {"skus": 0, "low_stock": 0, "units_on_hand": 0}
        )
        rows.append(
            {
                "branch_id": str(b.branch_id),
                "name": b.name,
                "code": b.code,
                "district": b.district,
                "province": b.province,
                "is_active": bool(b.is_active),
                "revenue": cur["revenue"],
                "orders": cur["orders"],
                "paid_orders": cur["paid"],
                "customers": cur["customers"],
                "items_sold": items_by_branch.get(b.branch_id, 0),
                "avg_order_value": (
                    round(cur["revenue"] / cur["paid"], 2) if cur["paid"] else 0.0
                ),
                "cancelled_orders": cur["cancelled"],
                "cancellation_rate": (
                    round((cur["cancelled"] / cur["orders"]) * 100, 1) if cur["orders"] else 0.0
                ),
                "previous_revenue": prv["revenue"],
                "revenue_change": _pct_change(cur["revenue"], prv["revenue"]),
                "revenue_share": (
                    round((cur["revenue"] / total_revenue) * 100, 1) if total_revenue else 0.0
                ),
                "skus_stocked": stock["skus"],
                "units_on_hand": stock["units_on_hand"],
                "low_stock_alerts": stock["low_stock"],
            }
        )

    rows.sort(key=lambda r: r["revenue"], reverse=True)

    # Orders placed before post-office→branch routing existed have no branch and
    # would otherwise vanish from a view that claims to cover everything.
    unassigned = None
    if scope.is_super_admin:
        stmt = _in_window(
            select(
                _revenue_sum().label("revenue"),
                func.count(Order.order_id).label("orders"),
            ).where(Order.branch_id.is_(None)),
            (start, end),
        )
        row = (await db.execute(stmt)).one()
        if int(row.orders or 0) > 0:
            unassigned = {"revenue": float(row.revenue or 0), "orders": int(row.orders or 0)}

    return {
        "success": True,
        "data": {
            "branches": rows,
            "total_revenue": total_revenue,
            "total_orders": sum(v["orders"] for v in current.values()),
            "unassigned": unassigned,
            "range": window.as_dict(),
            "scope": _scope_dict(scope, scope.resolve(None)),
        },
    }


@router.get("/top-products", response_model=dict)
async def analytics_top_products(
    days: int = DaysQuery,
    limit: int = LimitQuery,
    branch_id: Optional[UUID] = BranchQuery,
    db: AsyncSession = Depends(get_db),
    scope: BranchScope = Depends(inject_branch_filter),
):
    """
    Best sellers by revenue in the window.

    Reads product_name off ``order_items``, not ``products``: the line item
    stores the name as sold, so a renamed or deleted product still reports
    correctly against the period it actually sold in.
    """
    effective_branch = scope.resolve(branch_id)
    window = Window(days)

    stmt = (
        _in_window(
            _scoped(
                select(
                    OrderItem.product_id,
                    func.max(OrderItem.product_name).label("name"),
                    func.max(OrderItem.product_sku).label("sku"),
                    func.max(Category.name).label("category"),
                    func.coalesce(func.sum(OrderItem.quantity), 0).label("units"),
                    func.coalesce(func.sum(OrderItem.subtotal), 0).label("revenue"),
                    func.count(distinct(OrderItem.order_id)).label("orders"),
                )
                .select_from(OrderItem)
                .join(Order, Order.order_id == OrderItem.order_id)
                .outerjoin(Product, Product.product_id == OrderItem.product_id)
                .outerjoin(Category, Category.category_id == Product.category_id)
                .where(_is_revenue()),
                effective_branch,
            ),
            window.bounds,
        )
        .group_by(OrderItem.product_id)
        .order_by(func.coalesce(func.sum(OrderItem.subtotal), 0).desc())
        .limit(limit)
    )

    rows = [
        {
            "product_id": str(r.product_id),
            "name": r.name,
            "sku": r.sku,
            "category": r.category,
            "units_sold": int(r.units or 0),
            "revenue": float(r.revenue or 0),
            "orders": int(r.orders or 0),
        }
        for r in (await db.execute(stmt)).all()
    ]

    return {
        "success": True,
        "data": {
            "products": rows,
            "range": window.as_dict(),
            "scope": _scope_dict(scope, effective_branch),
        },
    }


@router.get("/top-customers", response_model=dict)
async def analytics_top_customers(
    days: int = DaysQuery,
    limit: int = LimitQuery,
    branch_id: Optional[UUID] = BranchQuery,
    db: AsyncSession = Depends(get_db),
    scope: BranchScope = Depends(inject_branch_filter),
):
    """Highest-spending customers in the window, ranked by revenue."""
    effective_branch = scope.resolve(branch_id)
    window = Window(days)

    stmt = (
        _in_window(
            _scoped(
                select(
                    Order.user_id,
                    func.max(User.full_name).label("full_name"),
                    func.max(User.email).label("email"),
                    func.max(User.phone).label("phone"),
                    _revenue_sum().label("revenue"),
                    func.count(Order.order_id).label("orders"),
                    func.max(Order.created_at).label("last_order_at"),
                )
                .select_from(Order)
                .outerjoin(User, User.user_id == Order.user_id),
                effective_branch,
            ),
            window.bounds,
        )
        .group_by(Order.user_id)
        .order_by(_revenue_sum().desc())
        .limit(limit)
    )

    rows = [
        {
            "user_id": str(r.user_id),
            "full_name": r.full_name or "Unknown customer",
            "email": r.email,
            "phone": r.phone,
            "revenue": float(r.revenue or 0),
            "orders": int(r.orders or 0),
            "last_order_at": r.last_order_at.isoformat() if r.last_order_at else None,
        }
        for r in (await db.execute(stmt)).all()
    ]

    return {
        "success": True,
        "data": {
            "customers": rows,
            "range": window.as_dict(),
            "scope": _scope_dict(scope, effective_branch),
        },
    }


@router.get("/categories", response_model=dict)
async def analytics_categories(
    days: int = DaysQuery,
    branch_id: Optional[UUID] = BranchQuery,
    db: AsyncSession = Depends(get_db),
    scope: BranchScope = Depends(inject_branch_filter),
):
    """Revenue split by top-level category, for the sales-mix chart."""
    effective_branch = scope.resolve(branch_id)
    window = Window(days)

    stmt = (
        _in_window(
            _scoped(
                select(
                    Category.category_id,
                    func.max(Category.name).label("name"),
                    func.coalesce(func.sum(OrderItem.subtotal), 0).label("revenue"),
                    func.coalesce(func.sum(OrderItem.quantity), 0).label("units"),
                )
                .select_from(OrderItem)
                .join(Order, Order.order_id == OrderItem.order_id)
                .join(Product, Product.product_id == OrderItem.product_id)
                .join(Category, Category.category_id == Product.category_id)
                .where(_is_revenue()),
                effective_branch,
            ),
            window.bounds,
        )
        .group_by(Category.category_id)
        .order_by(func.coalesce(func.sum(OrderItem.subtotal), 0).desc())
    )

    raw = (await db.execute(stmt)).all()
    total = sum(float(r.revenue or 0) for r in raw)
    rows = [
        {
            "category_id": str(r.category_id),
            "name": r.name,
            "revenue": float(r.revenue or 0),
            "units_sold": int(r.units or 0),
            "share": round((float(r.revenue or 0) / total) * 100, 1) if total else 0.0,
        }
        for r in raw
    ]

    return {
        "success": True,
        "data": {
            "categories": rows,
            "total_revenue": total,
            "range": window.as_dict(),
            "scope": _scope_dict(scope, effective_branch),
        },
    }


@router.get("/order-status", response_model=dict)
async def analytics_order_status(
    days: int = DaysQuery,
    branch_id: Optional[UUID] = BranchQuery,
    db: AsyncSession = Depends(get_db),
    scope: BranchScope = Depends(inject_branch_filter),
):
    """
    Order counts per fulfilment status, plus the headline fulfilment and
    cancellation rates operations teams are actually judged on.

    Every status in the enum is returned, zero-filled — a status that dropped to
    zero is information, and a table whose rows appear and disappear between
    refreshes is unreadable.
    """
    effective_branch = scope.resolve(branch_id)
    window = Window(days)

    stmt = _in_window(
        _scoped(
            select(
                Order.status,
                func.count(Order.order_id).label("orders"),
                _revenue_sum().label("revenue"),
            ),
            effective_branch,
        ),
        window.bounds,
    ).group_by(Order.status)

    found = {
        r.status: {"orders": int(r.orders or 0), "revenue": float(r.revenue or 0)}
        for r in (await db.execute(stmt)).all()
    }

    statuses = []
    for s in OrderStatus:
        entry = found.get(s.value, {"orders": 0, "revenue": 0.0})
        statuses.append(
            {"status": s.value, "orders": entry["orders"], "revenue": entry["revenue"]}
        )

    total_orders = sum(s["orders"] for s in statuses)
    delivered = found.get(OrderStatus.DELIVERED.value, {}).get("orders", 0)
    cancelled = found.get(OrderStatus.CANCELLED.value, {}).get("orders", 0)
    refunded = found.get(OrderStatus.REFUNDED.value, {}).get("orders", 0)

    # Payment-side split, for the "money stuck in pending" question.
    payment_stmt = _in_window(
        _scoped(
            select(Order.payment_status, func.count(Order.order_id).label("orders")),
            effective_branch,
        ),
        window.bounds,
    ).group_by(Order.payment_status)
    payment_found = {
        r.payment_status: int(r.orders or 0) for r in (await db.execute(payment_stmt)).all()
    }
    payments = [
        {"status": p.value, "orders": payment_found.get(p.value, 0)} for p in PaymentStatus
    ]

    return {
        "success": True,
        "data": {
            "statuses": statuses,
            "payments": payments,
            "total_orders": total_orders,
            "fulfilment_rate": (
                round((delivered / total_orders) * 100, 1) if total_orders else 0.0
            ),
            "cancellation_rate": (
                round(((cancelled + refunded) / total_orders) * 100, 1) if total_orders else 0.0
            ),
            "range": window.as_dict(),
            "scope": _scope_dict(scope, effective_branch),
        },
    }
