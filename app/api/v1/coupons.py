"""
Shopper-facing Coupons API Endpoints
"""
from datetime import datetime, timezone
from typing import Any, Dict, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from loguru import logger
from redis.asyncio import Redis
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.database import get_db
from app.config.redis import get_redis_client, get_redis_optional
from app.core.dependencies import get_current_user, get_current_user_optional
from app.models.coupon import Coupon
from app.services.coupon_service import CouponService
from app.services.loyalty_service import LoyaltyService

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


def _serialize_coupon(
    coupon: Coupon,
    collected: bool = False,
    is_first_order_eligible: bool = True,
    user_redemptions: int = 0,
) -> Dict[str, Any]:
    now = datetime.now(timezone.utc)
    valid_until = _as_utc(coupon.valid_until)
    valid_from = _as_utc(coupon.valid_from)

    is_expired = valid_until is not None and now > valid_until
    is_not_started = valid_from is not None and now < valid_from
    is_maxed = (
        coupon.usage_limit is not None
        and (coupon.used_count or 0) >= coupon.usage_limit
    )
    is_user_maxed = (
        coupon.per_user_limit is not None
        and user_redemptions >= coupon.per_user_limit
    )

    usable = (
        coupon.is_active
        and not is_expired
        and not is_not_started
        and not is_maxed
        and not is_user_maxed
    )

    if is_expired:
        state = "expired"
    elif is_user_maxed:
        state = "used"
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
        "first_order_only": bool(coupon.first_order_only),
        "exclude_quick_sale": bool(coupon.exclude_quick_sale),
        "eligible_categories": [
            str(cat.category_id) for cat in (coupon.eligible_categories or [])
        ],
        "per_user_limit": coupon.per_user_limit,
        "user_redemptions": user_redemptions,
        "is_first_order_eligible": is_first_order_eligible,
        "times_redeemed": coupon.used_count or 0,
        "collected": collected,
        "usable": usable,
        "state": state,
        # Members-only coupons name their level so the app can badge them.
        "min_tier": (
            {"name": coupon.min_tier.name, "color": coupon.min_tier.color}
            if getattr(coupon, "min_tier", None) is not None
            else None
        ),
    }


def _level_allows(coupon: Coupon, points: Optional[int]) -> bool:
    """A members-only coupon is shown only to members; guests never see one."""
    if coupon.min_tier_id is None:
        return True
    tier = coupon.min_tier
    return tier is not None and points is not None and points >= tier.min_points


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
            Coupon.is_public.is_(True),
            Coupon.target_user_id.is_(None),
            await _redeemable_here(redis, current_user),
        )
        result = await db.execute(stmt)
        coupons = result.scalars().all()
        points = (
            await LoyaltyService.get_points(db, current_user.user_id)
            if current_user
            else None
        )

        valid_coupons = []
        for c in coupons:
            if not _level_allows(c, points):
                continue
            vu = _as_utc(c.valid_until)
            vf = _as_utc(c.valid_from)
            if (vu is None or now <= vu) and (vf is None or now >= vf):
                is_coll = c.code.upper() in collected_codes
                user_redemptions = 0
                is_first_order_eligible = True
                if current_user:
                    user_redemptions = await CouponService.redemptions_by_user(
                        db, c.code, current_user.user_id
                    )
                    if c.first_order_only:
                        has_completed = await CouponService.has_completed_order(
                            db, current_user.user_id, c.branch_id
                        )
                        is_first_order_eligible = not has_completed

                valid_coupons.append(
                    _serialize_coupon(
                        c,
                        collected=is_coll,
                        is_first_order_eligible=is_first_order_eligible,
                        user_redemptions=user_redemptions,
                    )
                )

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

        # A code collected before the shopper switched delivery area is dropped
        # here rather than shown as unusable — the wallet only ever lists codes
        # that would survive checkout right now.
        #
        # Coupons issued to this customer (a level reward, an Assign Promo) are
        # theirs whether or not the Redis "collected" set heard about it: the
        # set is best-effort, and a reward lost to a cache blip is a promise
        # quietly broken.
        stmt = select(Coupon).where(
            Coupon.is_active.is_(True),
            await _redeemable_here(redis, current_user),
            or_(
                func.upper(Coupon.code).in_(sorted(collected_codes) or [""]),
                Coupon.target_user_id == current_user.user_id,
            ),
        )
        result = await db.execute(stmt)
        coupons = result.scalars().all()
        points = await LoyaltyService.get_points(db, current_user.user_id)

        user_coupons = []
        for c in coupons:
            if _level_allows(c, points):
                user_redemptions = await CouponService.redemptions_by_user(
                    db, c.code, current_user.user_id
                )
                is_first_order_eligible = True
                if c.first_order_only:
                    has_completed = await CouponService.has_completed_order(
                        db, current_user.user_id, c.branch_id
                    )
                    is_first_order_eligible = not has_completed

                user_coupons.append(
                    _serialize_coupon(
                        c,
                        collected=True,
                        is_first_order_eligible=is_first_order_eligible,
                        user_redemptions=user_redemptions,
                    )
                )

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
