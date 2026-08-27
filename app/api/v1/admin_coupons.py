"""
Admin Coupons API (Promotions & Coupons Management)

Global CRUD over promotional coupon codes. Restricted to super_admin and
marketing_manager (enforced at the router level). Coupons are not branch-scoped.

DELETE is a soft delete (sets is_active = False) so redemption history and any
orders referencing the code stay intact.

Prefix "/admin/coupons" is applied by app/api/v1/router.py.
"""
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from loguru import logger
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.database import get_db
from app.core.dependencies import require_roles
from app.models.coupon import Coupon
from app.schemas.coupon import CouponCreate, CouponUpdate

router = APIRouter(
    dependencies=[Depends(require_roles("super_admin", "marketing_manager"))],
    tags=["Admin Coupons"],
)


def _format_coupon(c: Coupon) -> dict:
    return {
        "coupon_id": str(c.coupon_id),
        "code": c.code,
        "description": c.description,
        "discount_type": c.discount_type,
        "discount_value": float(c.discount_value or 0),
        "min_order_value": float(c.min_order_value or 0),
        "usage_limit": c.usage_limit,
        "used_count": c.used_count or 0,
        "valid_from": c.valid_from.isoformat() if c.valid_from else None,
        "valid_until": c.valid_until.isoformat() if c.valid_until else None,
        "is_active": c.is_active,
        "created_at": c.created_at.isoformat() if c.created_at else None,
    }


async def _get_or_404(db: AsyncSession, coupon_id: UUID) -> Coupon:
    coupon = (
        await db.execute(select(Coupon).where(Coupon.coupon_id == coupon_id))
    ).scalar_one_or_none()
    if coupon is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Coupon not found")
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
    is_active: Optional[bool] = Query(None, description="Filter by status"),
    db: AsyncSession = Depends(get_db),
):
    """Paginated coupon list."""
    query = select(Coupon)
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

    return {
        "success": True,
        "data": {
            "coupons": [_format_coupon(c) for c in coupons],
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
):
    """Create a coupon."""
    if await _code_taken(db, data.code):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="A coupon with this code already exists",
        )

    coupon = Coupon(
        code=data.code,
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
    await db.commit()
    await db.refresh(coupon)
    logger.info(f"[admin] Coupon created: {coupon.code}")
    return {"success": True, "data": _format_coupon(coupon), "message": "Coupon created successfully"}


@router.put("/{coupon_id}", response_model=dict)
async def update_coupon(
    coupon_id: UUID,
    data: CouponUpdate,
    db: AsyncSession = Depends(get_db),
):
    """Edit a coupon."""
    coupon = await _get_or_404(db, coupon_id)
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

    for key, value in fields.items():
        if key == "discount_type" and value is not None:
            coupon.discount_type = value.value
        elif hasattr(coupon, key):
            setattr(coupon, key, value)

    await db.commit()
    await db.refresh(coupon)
    logger.info(f"[admin] Coupon updated: {coupon.coupon_id}")
    return {"success": True, "data": _format_coupon(coupon), "message": "Coupon updated successfully"}


@router.delete("/{coupon_id}", response_model=dict)
async def deactivate_or_delete_coupon(
    coupon_id: UUID,
    hard: bool = Query(False, description="Hard delete from database"),
    db: AsyncSession = Depends(get_db),
):
    """Deactivate (soft delete) or hard delete if hard=True."""
    coupon = await _get_or_404(db, coupon_id)
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
