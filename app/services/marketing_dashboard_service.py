"""
Marketing Dashboard Service

Everything behind ``GET /admin/marketing/dashboard`` — the branch Marketing
Manager's landing page.

Design rule for this module: **every number returned here is derived from a
real row.** The previous dashboard displayed a banner CTR, a "perishable
items" count and a delivery cut-off countdown that no table could produce, so
a manager acting on them was acting on decoration. Where a figure genuinely
cannot be computed (a branch that has not configured its delivery runs, a
banner with no impressions yet) the payload says so explicitly with ``null``
rather than substituting a plausible-looking zero — the UI renders those as
"not set up" instead of as a bad result.

All queries are scoped to a single ``branch_id``, resolved by the caller from
the authenticated admin (never from the request body).
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Optional
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import Integer, case, func, literal, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.banner import Banner, BannerEvent, BannerEventType
from app.models.branch import Branch
from app.models.coupon import Coupon
from app.models.order import Order, OrderStatus
from app.models.product import BranchInventory, Product
from app.services.product_service import ProductService

# Sri Lanka has a single timezone with no DST, so a fixed zone is correct here
# rather than a per-branch setting.
LK_TZ = ZoneInfo("Asia/Colombo")

# Rolling windows. 30 days for the retention rate itself (long enough for a
# weekly grocery shopper to have placed a second order), 7 days for the trend
# arrow beside it.
RETENTION_WINDOW_DAYS = 30
TREND_WINDOW_DAYS = 7

# Banner CTR is only meaningful windowed — see BannerEvent's docstring.
BANNER_CTR_WINDOW_DAYS = 7

# Below this many impressions a CTR percentage is noise (1 click out of 3 views
# is not a 33% CTR), so it is reported as null and the UI shows "collecting".
BANNER_CTR_MIN_IMPRESSIONS = 50

# A coupon this far through its redemption allowance is worth a manager's
# attention — it is about to stop working for customers who still see the code.
COUPON_NEAR_LIMIT_RATIO = 0.8

# Only a completed sale counts as a purchase for retention purposes. A
# cancelled or refunded order is not a customer coming back.
_COMPLETED_STATUSES = (OrderStatus.DELIVERED.value,)


def _utc_cutoff(days: int) -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=days)


# ============================================================================
# 1. Quick Sale — live deals
# ============================================================================

async def quick_sale_summary(
    db: AsyncSession, branch_id: UUID, limit: int = 6
) -> dict[str, Any]:
    """
    The branch's live Quick Sale deals, plus how many of them are perishable.

    ``live_count`` is the true total across the branch, not ``len(items)`` —
    the widget shows only the first few rows but the KPI must count them all.
    """
    live_count = (
        await db.execute(
            select(func.count())
            .select_from(BranchInventory)
            .join(Product, Product.product_id == BranchInventory.product_id)
            .where(
                BranchInventory.branch_id == branch_id,
                BranchInventory.is_active.is_(True),
                BranchInventory.is_on_sale.is_(True),
                Product.is_active.is_(True),
            )
        )
    ).scalar_one()

    perishable_count = (
        await db.execute(
            select(func.count())
            .select_from(BranchInventory)
            .join(Product, Product.product_id == BranchInventory.product_id)
            .where(
                BranchInventory.branch_id == branch_id,
                BranchInventory.is_active.is_(True),
                BranchInventory.is_on_sale.is_(True),
                Product.is_active.is_(True),
                Product.is_perishable.is_(True),
            )
        )
    ).scalar_one()

    rows = await ProductService.get_quick_sale_products(db, branch_id, limit)

    items = []
    for row in rows:
        product = row["product"]
        eff = row["effective"]
        items.append(
            {
                "productId": str(product.product_id),
                "name": product.name,
                "isPerishable": bool(product.is_perishable),
                "globalPrice": eff["global_price"],
                "effectivePrice": eff["effective_price"],
                "effectiveDiscount": eff["effective_discount"],
                "effectiveDiscountPrice": eff["effective_discount_price"],
                "stockQuantity": eff["stock_quantity"],
            }
        )

    return {
        "liveCount": int(live_count),
        "perishableCount": int(perishable_count),
        "items": items,
    }


# ============================================================================
# 2. Home banners — real CTR over a rolling window
# ============================================================================

async def banner_performance(
    db: AsyncSession, branch_id: UUID, limit: int = 5
) -> dict[str, Any]:
    """
    Active banners on this branch's storefront with their measured CTR.

    A platform-wide banner (``Banner.branch_id IS NULL``) is included, but its
    impressions and clicks are filtered to *this* branch — a Colombo manager
    sees how the network banner performs in Colombo, which is the only version
    of that number they can act on.

    ``ctr`` is ``null`` until a banner has ``BANNER_CTR_MIN_IMPRESSIONS``
    impressions in the window; a percentage off three views is noise, and
    showing it as "0.0% — Low CTR" would push managers to retire banners that
    simply have not been seen yet.
    """
    since = _utc_cutoff(BANNER_CTR_WINDOW_DAYS)

    visible = or_(Banner.branch_id == branch_id, Banner.branch_id.is_(None))

    total_count = (
        await db.execute(
            select(func.count()).select_from(Banner).where(visible)
        )
    ).scalar_one()

    banners = (
        await db.execute(
            select(Banner)
            .where(visible, Banner.is_active.is_(True))
            .order_by(Banner.sort_order, Banner.created_at)
        )
    ).scalars().all()

    active_count = len(banners)
    if not banners:
        return {
            "activeCount": 0,
            "totalCount": int(total_count),
            "averageCtr": None,
            "windowDays": BANNER_CTR_WINDOW_DAYS,
            "items": [],
        }

    banner_ids = [b.banner_id for b in banners]
    stat_rows = (
        await db.execute(
            select(
                BannerEvent.banner_id,
                func.sum(
                    case(
                        (BannerEvent.event_type == BannerEventType.IMPRESSION.value, 1),
                        else_=0,
                    )
                ).label("impressions"),
                func.sum(
                    case(
                        (BannerEvent.event_type == BannerEventType.CLICK.value, 1),
                        else_=0,
                    )
                ).label("clicks"),
            )
            .where(
                BannerEvent.banner_id.in_(banner_ids),
                BannerEvent.branch_id == branch_id,
                BannerEvent.created_at >= since,
            )
            .group_by(BannerEvent.banner_id)
        )
    ).all()
    stats = {r.banner_id: (int(r.impressions or 0), int(r.clicks or 0)) for r in stat_rows}

    items = []
    total_impressions = 0
    total_clicks = 0
    for b in banners[:limit]:
        impressions, clicks = stats.get(b.banner_id, (0, 0))
        items.append(
            {
                "bannerId": str(b.banner_id),
                "title": b.title,
                "subtitle": b.subtitle,
                "imageUrl": b.image_url,
                "linkType": b.link_type,
                "isPlatformWide": b.branch_id is None,
                "impressions": impressions,
                "clicks": clicks,
                "ctr": (
                    round(clicks / impressions * 100, 1)
                    if impressions >= BANNER_CTR_MIN_IMPRESSIONS
                    else None
                ),
            }
        )

    # The branch average is computed over every active banner, not just the
    # `limit` shown, and pools the raw events rather than averaging per-banner
    # percentages — otherwise one barely-seen banner swings the headline.
    for impressions, clicks in stats.values():
        total_impressions += impressions
        total_clicks += clicks

    return {
        "activeCount": active_count,
        "totalCount": int(total_count),
        "averageCtr": (
            round(total_clicks / total_impressions * 100, 1)
            if total_impressions >= BANNER_CTR_MIN_IMPRESSIONS
            else None
        ),
        "windowDays": BANNER_CTR_WINDOW_DAYS,
        "items": items,
    }


# ============================================================================
# 3. Coupons
# ============================================================================

async def coupon_summary(db: AsyncSession, branch_id: UUID) -> dict[str, Any]:
    """
    Coupons currently redeemable at this branch: the branch's own, plus the
    network-wide ones a Super Admin published.

    "Near limit" is measured against ``usage_limit`` — the only cap the coupon
    model actually has. The previous dashboard warned about a "budget limit"
    that no column stores.
    """
    now = datetime.now(timezone.utc)
    redeemable_here = or_(Coupon.branch_id == branch_id, Coupon.branch_id.is_(None))
    live = (
        Coupon.is_active.is_(True),
        Coupon.valid_from <= now,
        Coupon.valid_until >= now,
        redeemable_here,
    )

    active_count = (
        await db.execute(select(func.count()).select_from(Coupon).where(*live))
    ).scalar_one()

    # used_count / usage_limit, only for coupons that actually have a cap.
    near_limit_rows = (
        await db.execute(
            select(Coupon.code, Coupon.used_count, Coupon.usage_limit).where(
                *live,
                Coupon.usage_limit.isnot(None),
                Coupon.usage_limit > 0,
                Coupon.used_count
                >= (func.cast(Coupon.usage_limit, Integer) * literal(COUPON_NEAR_LIMIT_RATIO)),
            )
        )
    ).all()

    return {
        "activeCount": int(active_count),
        "nearLimitCount": len(near_limit_rows),
        "nearLimit": [
            {
                "code": r.code,
                "usedCount": int(r.used_count or 0),
                "usageLimit": int(r.usage_limit),
            }
            for r in near_limit_rows
        ],
    }


# ============================================================================
# 4. Next delivery run
# ============================================================================

def _parse_hhmm(value: Any) -> Optional[time]:
    """Tolerate a malformed row in branches.delivery_runs rather than 500ing."""
    if not isinstance(value, str):
        return None
    try:
        hh, mm = value.split(":")[:2]
        return time(int(hh), int(mm))
    except (ValueError, TypeError):
        return None


def next_delivery_run(branch: Branch, now: Optional[datetime] = None) -> Optional[dict[str, Any]]:
    """
    The next dispatch round for this branch, with its ordering cut-off.

    Returns ``None`` when the branch has no runs configured — the dashboard
    then shows a "configure delivery runs" prompt instead of a fabricated
    2:00 PM, which is what the mock did.

    The chosen run is the next one whose **cut-off** is still in the future: a
    run whose cut-off has passed is already locked for ordering, so counting
    down to it would tell a manager nothing they can still act on. Once the
    last cut-off of the day passes, this rolls over to tomorrow's first run.
    """
    runs = branch.delivery_runs or []
    now = (now or datetime.now(timezone.utc)).astimezone(LK_TZ)
    today = now.date()

    parsed: list[tuple[time, str, int]] = []
    for entry in runs:
        if not isinstance(entry, dict):
            continue
        dispatch = _parse_hhmm(entry.get("dispatch_time"))
        if dispatch is None:
            continue
        try:
            cutoff_minutes = int(entry.get("cutoff_minutes", 0))
        except (TypeError, ValueError):
            cutoff_minutes = 0
        parsed.append((dispatch, str(entry.get("label") or "Delivery run"), max(0, cutoff_minutes)))

    if not parsed:
        return None

    parsed.sort(key=lambda r: r[0])

    def _build(run_date: date, run: tuple[time, str, int]) -> dict[str, Any]:
        dispatch_at = datetime.combine(run_date, run[0], tzinfo=LK_TZ)
        cutoff_at = dispatch_at - timedelta(minutes=run[2])
        return {
            "label": run[1],
            "dispatchAt": dispatch_at.isoformat(),
            "cutoffAt": cutoff_at.isoformat(),
            "minutesToCutoff": int((cutoff_at - now).total_seconds() // 60),
            "isTomorrow": run_date != today,
        }

    for run in parsed:
        candidate = _build(today, run)
        if candidate["minutesToCutoff"] >= 0:
            return candidate

    # Every cut-off today has passed — the next actionable run is tomorrow's first.
    return _build(today + timedelta(days=1), parsed[0])


# ============================================================================
# 5. Returning Customer Rate
# ============================================================================

async def _returning_rate_for_window(
    db: AsyncSession, branch_id: UUID, start: datetime, end: datetime
) -> tuple[Optional[float], int, int]:
    """
    Returning Customer Rate over ``[start, end)`` for one branch.

                 repeat customers (2 or more completed orders)
        RCR  =  ------------------------------------------------  x 100
                total unique customers with a completed order

    Returns ``(rate, total_customers, repeat_customers)``. ``rate`` is ``None``
    when the branch had no completed orders in the window — a branch with zero
    customers has no retention rate, and reporting 0% would read as "everybody
    churned".

    Guest/unattributed orders (``user_id IS NULL``) are excluded: they cannot
    be recognised as the same person returning, so counting them would only
    dilute the denominator.
    """
    per_customer = (
        select(
            Order.user_id.label("user_id"),
            func.count(Order.order_id).label("order_count"),
        )
        .where(
            Order.branch_id == branch_id,
            Order.user_id.isnot(None),
            Order.status.in_(_COMPLETED_STATUSES),
            Order.created_at >= start,
            Order.created_at < end,
        )
        .group_by(Order.user_id)
        .subquery()
    )

    row = (
        await db.execute(
            select(
                func.count().label("total_customers"),
                func.sum(case((per_customer.c.order_count >= 2, 1), else_=0)).label(
                    "repeat_customers"
                ),
            ).select_from(per_customer)
        )
    ).one()

    total = int(row.total_customers or 0)
    repeat = int(row.repeat_customers or 0)
    if total == 0:
        return None, 0, 0
    return round(repeat / total * 100, 1), total, repeat


async def returning_customer_rate(db: AsyncSession, branch_id: UUID) -> dict[str, Any]:
    """
    Retention headline plus its week-over-week trend.

    The headline is the rolling 30-day rate. The trend compares the last 7 days
    against the 7 before that, as a percentage-point difference:

        trend = RCR(last 7 days) - RCR(previous 7 days)

    ``trend`` is ``null`` when either week had no completed orders — there is
    no honest comparison to draw, and rendering "▲ +42%" off a single order
    would be worse than rendering nothing.
    """
    now = datetime.now(timezone.utc)

    rate, total, repeat = await _returning_rate_for_window(
        db, branch_id, now - timedelta(days=RETENTION_WINDOW_DAYS), now
    )

    current_rate, _, _ = await _returning_rate_for_window(
        db, branch_id, now - timedelta(days=TREND_WINDOW_DAYS), now
    )
    previous_rate, _, _ = await _returning_rate_for_window(
        db,
        branch_id,
        now - timedelta(days=TREND_WINDOW_DAYS * 2),
        now - timedelta(days=TREND_WINDOW_DAYS),
    )

    trend = (
        round(current_rate - previous_rate, 1)
        if current_rate is not None and previous_rate is not None
        else None
    )

    return {
        "rate": rate,
        "trend": trend,
        "windowDays": RETENTION_WINDOW_DAYS,
        "trendWindowDays": TREND_WINDOW_DAYS,
        "totalCustomers": total,
        "repeatCustomers": repeat,
    }


# ============================================================================
# Aggregate
# ============================================================================

async def build_dashboard(db: AsyncSession, branch: Branch) -> dict[str, Any]:
    """Assemble the whole Marketing Dashboard payload for one branch."""
    quick_sale = await quick_sale_summary(db, branch.branch_id)
    banners = await banner_performance(db, branch.branch_id)
    coupons = await coupon_summary(db, branch.branch_id)
    retention = await returning_customer_rate(db, branch.branch_id)

    return {
        "branch": {
            "branchId": str(branch.branch_id),
            "branchName": branch.name,
        },
        "quickSale": quick_sale,
        "banners": banners,
        "coupons": coupons,
        "nextDeliveryRun": next_delivery_run(branch),
        "returningCustomers": retention,
    }
