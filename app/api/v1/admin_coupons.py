"""
Admin Coupons API (Promotions & Coupons Management)

CRUD over promotional coupon codes. Restricted to super_admin and
marketing_manager (enforced at the router level).

Branch scoping: a coupon belongs to one branch, or to the whole network when
``branch_id IS NULL``. A branch's Marketing Manager sees their own branch's
coupons plus the network-wide ones, but may only create or modify their own —
otherwise one branch's promotion would be redeemable against, and paid for by,
every other branch. Only a Super Admin publishes network-wide.

DELETE is a soft delete (sets is_active = False) so redemption history and any
orders referencing the code stay intact.

Prefix "/admin/coupons" is applied by app/api/v1/router.py.
"""
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from loguru import logger
from sqlalchemy import delete, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from datetime import datetime, timezone
from decimal import Decimal

from app.config.database import get_db
from app.core.dependencies import BranchScope, inject_branch_filter, require_roles
from app.models.coupon import Coupon, CouponCategory, CouponProduct
from app.models.order import Order, OrderStatus
from app.schemas.coupon import CouponCreate, CouponUpdate

router = APIRouter(
    dependencies=[Depends(require_roles("super_admin", "marketing_manager"))],
    tags=["Admin Coupons"],
)


# Orders in these states cost the branch nothing, so they contribute to
# neither the budget spent nor the revenue a campaign is credited with.
_VOID_STATUSES = (
    OrderStatus.CANCELLED.value,
    OrderStatus.REFUNDED.value,
    OrderStatus.RTO_INITIATED.value,
)


def _derive_status(c: Coupon, now: datetime) -> str:
    """
    The single status the admin table filters and badges on.

    Four mutually exclusive outcomes, ordered by what a manager needs to know
    first. The distinction that matters most is 'depleted' vs 'inactive':
    a depleted campaign worked so well it spent its allowance, an inactive one
    was switched off by a person. Collapsing them into "not running" loses the
    only signal that says "consider funding this again".
    """
    if c.is_budget_depleted:
        return "depleted"
    if not c.is_active:
        return "inactive"
    if c.valid_until is not None and _aware(c.valid_until) < now:
        return "expired"
    if c.valid_from is not None and _aware(c.valid_from) > now:
        return "scheduled"
    return "active"


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _format_coupon(c: Coupon, performance: dict | None = None) -> dict:
    now = datetime.now(timezone.utc)
    budget_cap = float(c.budget_cap) if c.budget_cap is not None else None
    budget_spent = float(c.budget_spent or 0)
    perf = performance or {}
    return {
        "coupon_id": str(c.coupon_id),
        "code": c.code,
        "branch_id": str(c.branch_id) if c.branch_id else None,
        "is_network_wide": c.branch_id is None,
        # Derived, not stored: a coupon becomes expired or depleted by the
        # passage of time and the spending of money, not by anyone writing a
        # column. Storing it would mean a row that is wrong until a job runs.
        "status": _derive_status(c, now),
        "budget_cap": budget_cap,
        "budget_spent": budget_spent,
        "budget_used_percent": (
            round(budget_spent / budget_cap * 100, 1)
            if budget_cap and budget_cap > 0
            else None
        ),
        "auto_stop_on_budget": c.auto_stop_on_budget,
        "exclude_quick_sale": c.exclude_quick_sale,
        "first_order_only": c.first_order_only,
        "per_user_limit": c.per_user_limit,
        "is_public": c.is_public,
        "target_user_id": str(c.target_user_id) if c.target_user_id else None,
        "category_ids": [str(x.category_id) for x in (c.eligible_categories or [])],
        "product_ids": [str(x.product_id) for x in (c.eligible_products or [])],
        # Revenue the code actually brought in, and over how many orders. This
        # is the other half of the budget figure: Rs 30,000 of discount that
        # pulled in Rs 612,000 of sales is a success, and the spend column
        # alone cannot say so.
        "revenue_generated": perf.get("revenue", 0.0),
        "orders_count": perf.get("orders", 0),
        "description": c.description,
        "discount_type": c.discount_type,
        "discount_value": float(c.discount_value or 0),
        "min_order_value": float(c.min_order_value or 0),
        "max_discount_amount": float(c.max_discount_amount) if getattr(c, "max_discount_amount", None) is not None else None,
        "usage_limit": c.usage_limit,
        "used_count": c.used_count or 0,
        "valid_from": c.valid_from.isoformat() if c.valid_from else None,
        "valid_until": c.valid_until.isoformat() if c.valid_until else None,
        "is_active": c.is_active,
        "created_at": c.created_at.isoformat() if c.created_at else None,
    }


async def _performance_map(db: AsyncSession, coupons: list[Coupon]) -> dict[str, dict]:
    """
    Revenue and order count per coupon code, in one grouped query.

    Aggregated from ``orders`` rather than kept as a counter because, unlike
    the budget, nothing at checkout has to test it under a lock — so the
    reconcilable source of truth can be read directly and there is no
    denormalised copy to drift.

    Cancelled, refunded and RTO'd orders are excluded: a campaign should not be
    credited with revenue that was handed back.
    """
    codes = [c.code for c in coupons if c.code]
    if not codes:
        return {}

    rows = (
        await db.execute(
            select(
                func.lower(Order.coupon_code).label("code"),
                func.coalesce(func.sum(Order.total_amount), 0).label("revenue"),
                func.count(Order.order_id).label("orders"),
            )
            .where(
                func.lower(Order.coupon_code).in_([c.lower() for c in codes]),
                Order.status.notin_(_VOID_STATUSES),
            )
            .group_by(func.lower(Order.coupon_code))
        )
    ).all()
    return {
        r.code: {"revenue": float(r.revenue or 0), "orders": int(r.orders or 0)}
        for r in rows
    }


async def _sync_eligibility(
    db: AsyncSession,
    coupon: Coupon,
    category_ids: list | None,
    product_ids: list | None,
) -> None:
    """
    Replace a coupon's catalog-eligibility rows.

    ``None`` means "not mentioned in this request" and leaves the set alone; an
    empty list means "no restriction", which is the only way to widen a coupon
    back to the whole catalog once it has been narrowed.
    """
    if category_ids is not None:
        await db.execute(
            delete(CouponCategory).where(CouponCategory.coupon_id == coupon.coupon_id)
        )
        for cid in category_ids:
            db.add(CouponCategory(coupon_id=coupon.coupon_id, category_id=cid))
    if product_ids is not None:
        await db.execute(
            delete(CouponProduct).where(CouponProduct.coupon_id == coupon.coupon_id)
        )
        for pid in product_ids:
            db.add(CouponProduct(coupon_id=coupon.coupon_id, product_id=pid))


def _visible_to(scope: BranchScope):
    """
    Rows a caller may *see*: their own branch's coupons plus the network-wide
    ones. An unscoped caller (Super Admin) sees everything.
    """
    if scope.is_unscoped:
        return None
    return or_(Coupon.branch_id == scope.branch_id, Coupon.branch_id.is_(None))


async def _get_or_404(
    db: AsyncSession, coupon_id: UUID, scope: BranchScope, for_write: bool = False
) -> Coupon:
    """
    Fetch a coupon the caller is allowed to act on.

    A scoped caller gets 404 (not 403) for another branch's coupon: the
    existence of a rival branch's promo code is itself information they have no
    business confirming. ``for_write`` additionally refuses the network-wide
    rows they can see but must not edit — that refusal *is* explained, since
    they can see the row and would otherwise be told it vanished.
    """
    stmt = select(Coupon).where(Coupon.coupon_id == coupon_id)
    visible = _visible_to(scope)
    if visible is not None:
        stmt = stmt.where(visible)

    coupon = (await db.execute(stmt)).scalar_one_or_none()
    if coupon is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Coupon not found")

    if for_write and not scope.is_unscoped and coupon.branch_id is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Network-wide coupons can only be modified by a Super Admin.",
        )
    return coupon


async def _code_taken(db: AsyncSession, code: str, exclude_id: Optional[UUID] = None) -> bool:
    stmt = select(Coupon.coupon_id).where(func.lower(Coupon.code) == code.lower())
    if exclude_id is not None:
        stmt = stmt.where(Coupon.coupon_id != exclude_id)
    return (await db.execute(stmt)).scalar_one_or_none() is not None


@router.get("", response_model=dict)
async def list_coupons(
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    search: Optional[str] = Query(None, description="Match code or description"),
    is_active: Optional[bool] = Query(None, description="Filter by the raw is_active flag"),
    status_filter: Optional[str] = Query(
        None,
        alias="status",
        pattern="^(active|scheduled|expired|inactive|depleted)$",
        description=(
            "Filter by derived lifecycle status. 'depleted' means the campaign "
            "budget is exhausted, which is distinct from someone switching it off."
        ),
    ),
    db: AsyncSession = Depends(get_db),
    scope: BranchScope = Depends(inject_branch_filter),
):
    """
    Paginated coupon list.

    Branch-scoped: a Marketing Manager sees their own branch's coupons and the
    network-wide ones, never another branch's.
    """
    query = select(Coupon)
    visible = _visible_to(scope)
    if visible is not None:
        query = query.where(visible)
    if is_active is not None:
        query = query.where(Coupon.is_active == is_active)
    if search:
        pattern = f"%{search}%"
        query = query.where(
            or_(Coupon.code.ilike(pattern), Coupon.description.ilike(pattern))
        )

    total = (await db.execute(select(func.count()).select_from(query.subquery()))).scalar() or 0

    offset = (page - 1) * limit
    query = query.order_by(Coupon.created_at.desc()).limit(limit).offset(offset)
    coupons = (await db.execute(query)).scalars().all()

    # One grouped query for the whole page rather than one per row.
    perf = await _performance_map(db, list(coupons))
    formatted = [_format_coupon(c, perf.get(c.code.lower())) for c in coupons]

    # `status` is derived (see _derive_status), so it cannot be a SQL WHERE
    # clause without duplicating that logic in two languages. Filtering the
    # page in Python keeps one definition; the page size cap (100) bounds it.
    if status_filter:
        formatted = [row for row in formatted if row["status"] == status_filter]

    return {
        "success": True,
        "data": {
            "coupons": formatted,
            "pagination": {
                "total": total,
                "page": page,
                "limit": limit,
                "total_pages": (total + limit - 1) // limit if limit else 1,
            },
        },
    }


@router.post("", response_model=dict, status_code=status.HTTP_201_CREATED)
async def create_coupon(
    data: CouponCreate,
    db: AsyncSession = Depends(get_db),
    scope: BranchScope = Depends(inject_branch_filter),
):
    """
    Create a coupon.

    A scoped creator's coupon is always pinned to their own branch — a
    ``branch_id`` in the body is ignored rather than trusted, mirroring how
    admin_banners.py pins a banner. Only a Super Admin may create a
    network-wide coupon, by omitting ``branch_id``.
    """
    if await _code_taken(db, data.code):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="A coupon with this code already exists",
        )

    branch_id = data.branch_id if scope.is_unscoped else scope.branch_id

    coupon = Coupon(
        code=data.code,
        branch_id=branch_id,
        budget_cap=data.budget_cap,
        auto_stop_on_budget=data.auto_stop_on_budget,
        exclude_quick_sale=data.exclude_quick_sale,
        first_order_only=data.first_order_only,
        per_user_limit=data.per_user_limit,
        is_public=data.is_public,
        description=data.description,
        discount_type=data.discount_type.value,
        discount_value=data.discount_value,
        min_order_value=data.min_order_value,
        usage_limit=data.usage_limit,
        valid_from=data.valid_from,
        valid_until=data.valid_until,
        is_active=data.is_active,
    )
    db.add(coupon)
    await db.flush()
    await _sync_eligibility(db, coupon, data.category_ids, data.product_ids)
    await db.commit()
    await db.refresh(coupon)
    logger.info(
        f"[admin] Coupon created: {coupon.code} "
        f"({'network-wide' if branch_id is None else f'branch {branch_id}'})"
    )
    return {"success": True, "data": _format_coupon(coupon), "message": "Coupon created successfully"}


@router.put("/{coupon_id}", response_model=dict)
async def update_coupon(
    coupon_id: UUID,
    data: CouponUpdate,
    db: AsyncSession = Depends(get_db),
    scope: BranchScope = Depends(inject_branch_filter),
):
    """Edit a coupon. A scoped admin may only edit their own branch's."""
    coupon = await _get_or_404(db, coupon_id, scope, for_write=True)
    # branch_id is not in CouponUpdate at all, so a coupon can never change
    # owner through this route — there is no legitimate reason to hand a live
    # promo code to a different branch mid-campaign.
    fields = data.model_dump(exclude_unset=True)

    if "code" in fields and fields["code"]:
        if await _code_taken(db, fields["code"], exclude_id=coupon_id):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="A coupon with this code already exists",
            )

    # Resolve the effective values for cross-field validation (a single-sided
    # date/type/value change must still be consistent with what's stored).
    new_from = fields.get("valid_from", coupon.valid_from)
    new_until = fields.get("valid_until", coupon.valid_until)
    if new_until <= new_from:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="valid_until must be after valid_from",
        )

    new_type = fields.get("discount_type")
    new_type_val = new_type.value if new_type is not None else coupon.discount_type
    new_value = fields.get("discount_value", coupon.discount_value)
    if new_value is not None and new_value <= 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="discount_value must be greater than 0",
        )
    if new_type_val == "percentage" and new_value is not None and new_value > 100:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Percentage discount cannot exceed 100",
        )

    # Same rule CouponCreate enforces at creation time: a fixed discount can
    # never exceed the order it applies to. A partial update can change either
    # side without resending the other, so check the value the row *ends up*
    # with, not just what this request happened to include.
    new_min_order_value = fields.get("min_order_value", coupon.min_order_value)
    if new_type_val == "fixed" and new_value is not None and new_min_order_value < new_value:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="min_order_value must be greater than or equal to discount_value for fixed discounts",
        )

    # The eligibility sets are join tables, not columns — pop them out before
    # the generic setattr loop so it does not try to assign a list to the ORM
    # relationship and silently detach the existing rows.
    category_ids = fields.pop("category_ids", None)
    product_ids = fields.pop("product_ids", None)

    for key, value in fields.items():
        if key == "discount_type" and value is not None:
            coupon.discount_type = value.value
        elif hasattr(coupon, key):
            setattr(coupon, key, value)

    await _sync_eligibility(db, coupon, category_ids, product_ids)

    await db.commit()
    await db.refresh(coupon)
    logger.info(f"[admin] Coupon updated: {coupon.coupon_id}")
    return {"success": True, "data": _format_coupon(coupon), "message": "Coupon updated successfully"}


@router.delete("/{coupon_id}", response_model=dict)
async def deactivate_or_delete_coupon(
    coupon_id: UUID,
    hard: bool = Query(False, description="Hard delete from database"),
    db: AsyncSession = Depends(get_db),
    scope: BranchScope = Depends(inject_branch_filter),
):
    """Deactivate (soft delete) or hard delete if hard=True."""
    coupon = await _get_or_404(db, coupon_id, scope, for_write=True)
    if hard:
        try:
            await db.delete(coupon)
            await db.commit()
            logger.info(f"[admin] Coupon hard deleted: {coupon_id}")
            return {"success": True, "message": "Coupon deleted permanently"}
        except IntegrityError:
            await db.rollback()
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Cannot permanently delete coupon as it has been redeemed in past orders. Deactivate it instead.",
            )
    else:
        coupon.is_active = False
        await db.commit()
        logger.info(f"[admin] Coupon deactivated: {coupon_id}")
        return {"success": True, "message": "Coupon deactivated successfully"}
