"""
Shopper-facing Coupons API Endpoints
"""
from datetime import datetime, timezone
from typing import Any, Dict, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from loguru import logger
from redis.asyncio import Redis
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.database import get_db
from app.config.redis import get_redis_client, get_redis_optional
from app.core.dependencies import get_current_user, get_current_user_optional
from app.models.coupon import Coupon
from app.services.coupon_service import CouponService

router = APIRouter(tags=["Coupons"])


async def _redeemable_here(redis: Optional[Redis], current_user):
    """
    SQL filter limiting coupons to the ones this shopper could actually use:
    their delivery branch's own, plus the network-wide ones.

    Showing a shopper a code that will be refused at checkout is worse than
    showing them nothing, so this filters rather than annotates. A shopper with
    no branch context yet (guest, or no delivery location chosen) sees only the
    network-wide offers — those are valid wherever they end up ordering from.
    """
    branch_id = None
    if redis is not None and current_user is not None:
        try:
            from app.services import branch_service

            context = await branch_service.get_branch_context(
                redis, str(current_user.user_id)
            )
            if context and context.get("branch_id"):
                branch_id = UUID(context["branch_id"])
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(f"Could not resolve branch context for coupons: {exc}")

    if branch_id is None:
        return Coupon.branch_id.is_(None)
    return or_(Coupon.branch_id == branch_id, Coupon.branch_id.is_(None))


def _as_utc(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _serialize_coupon(coupon: Coupon, collected: bool = False) -> Dict[str, Any]:
    now = datetime.now(timezone.utc)
    valid_until = _as_utc(coupon.valid_until)
    valid_from = _as_utc(coupon.valid_from)

    is_expired = valid_until is not None and now > valid_until
    is_not_started = valid_from is not None and now < valid_from
    is_maxed = (
        coupon.usage_limit is not None
        and (coupon.used_count or 0) >= coupon.usage_limit
    )

    usable = (
        coupon.is_active and not is_expired and not is_not_started and not is_maxed
    )

    if is_expired:
        state = "expired"
    elif collected:
        state = "collected"
    else:
        state = "available"

    return {
        "coupon_id": str(coupon.coupon_id),
        "code": coupon.code,
        "description": coupon.description,
        "discount_type": coupon.discount_type,
        "discount_value": (
            float(coupon.discount_value) if coupon.discount_value is not None else 0.0
        ),
        "min_order_value": (
            float(coupon.min_order_value)
            if coupon.min_order_value is not None
            else 0.0
        ),
        "max_discount_amount": (
            float(coupon.max_discount_amount)
            if coupon.max_discount_amount is not None
            else None
        ),
        "valid_until": (
            coupon.valid_until.isoformat() if coupon.valid_until else None
        ),
        "per_user_limit": 1,
        "times_redeemed": coupon.used_count or 0,
        "collected": collected,
        "usable": usable,
        "state": state,
    }


async def _get_collected_codes(user_id: Optional[str]) -> set:
    if not user_id:
        return set()
    r = get_redis_client()
    if r is None:
        return set()
    try:
        members = await r.smembers(f"user:{user_id}:collected_coupons")
        return set(members or [])
    except Exception as e:
        logger.warning(f"Failed to fetch collected coupons from Redis: {e}")
        return set()


async def _add_collected_code(user_id: str, code: str) -> None:
    r = get_redis_client()
    if r is None:
        return
    try:
        await r.sadd(f"user:{user_id}:collected_coupons", code.upper())
    except Exception as e:
        logger.warning(f"Failed to save collected coupon to Redis: {e}")


@router.get("/available", response_model=dict)
async def get_available_coupons(
    db: AsyncSession = Depends(get_db),
    redis: Optional[Redis] = Depends(get_redis_optional),
    current_user=Depends(get_current_user_optional),
):
    """
    Public offers this shopper can actually redeem: their delivery branch's
    coupons plus the network-wide ones.
    """
    try:
        now = datetime.now(timezone.utc)
        user_id = str(current_user.user_id) if current_user else None
        collected_codes = await _get_collected_codes(user_id)

        stmt = select(Coupon).where(
            Coupon.is_active.is_(True),
            await _redeemable_here(redis, current_user),
        )
        result = await db.execute(stmt)
        coupons = result.scalars().all()

        valid_coupons = []
        for c in coupons:
            vu = _as_utc(c.valid_until)
            vf = _as_utc(c.valid_from)
            if (vu is None or now <= vu) and (vf is None or now >= vf):
                is_coll = c.code.upper() in collected_codes
                valid_coupons.append(_serialize_coupon(c, collected=is_coll))

        return {"success": True, "data": {"coupons": valid_coupons}}
    except Exception as e:
        logger.error(f"Error fetching available coupons: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to fetch available coupons",
        )


@router.get("/collected", response_model=dict)
async def get_collected_coupons(
    db: AsyncSession = Depends(get_db),
    redis: Optional[Redis] = Depends(get_redis_optional),
    current_user=Depends(get_current_user_optional),
):
    """
    Get customer's collected coupons.
    """
    try:
        if not current_user:
            return {"success": True, "data": {"coupons": []}}

        user_id = str(current_user.user_id)
        collected_codes = await _get_collected_codes(user_id)

        if not collected_codes:
            return {"success": True, "data": {"coupons": []}}

        # A code collected before the shopper switched delivery area is dropped
        # here rather than shown as unusable — the wallet only ever lists codes
        # that would survive checkout right now.
        stmt = select(Coupon).where(
            Coupon.is_active.is_(True),
            await _redeemable_here(redis, current_user),
        )
        result = await db.execute(stmt)
        coupons = result.scalars().all()

        user_coupons = []
        for c in coupons:
            if c.code.upper() in collected_codes:
                user_coupons.append(_serialize_coupon(c, collected=True))

        return {"success": True, "data": {"coupons": user_coupons}}
    except Exception as e:
        logger.error(f"Error fetching collected coupons: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to fetch collected coupons",
        )


@router.post("/collect", response_model=dict)
async def collect_coupon(
    payload: dict,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """
    Add a coupon code to the user's wallet.
    """
    try:
        code = payload.get("code")
        if not code or not code.strip():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Coupon code is required.",
            )

        code_clean = code.strip()
        coupon = await CouponService.get_by_code(db, code_clean)
        if not coupon:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Coupon code not found.",
            )

        now = datetime.now(timezone.utc)
        vu = _as_utc(coupon.valid_until)
        vf = _as_utc(coupon.valid_from)

        if not coupon.is_active or (vu and now > vu) or (vf and now < vf):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="This coupon is not active or has expired.",
            )

        user_id = str(current_user.user_id)
        await _add_collected_code(user_id, coupon.code)

        serialized = _serialize_coupon(coupon, collected=True)
        return {
            "success": True,
            "data": serialized,
            "message": "Offer added to My Coupons",
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error collecting coupon: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to collect coupon",
        )
