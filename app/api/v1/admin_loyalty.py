"""
SRIBEESonline - Admin Loyalty API

  GET    /tiers              — every level, with how many customers are on it
  POST   /tiers              — add a level                     (Super Admin)
  PUT    /tiers/{tier_id}    — change a level                  (Super Admin)
  DELETE /tiers/{tier_id}    — remove a level no coupon needs  (Super Admin)
  GET    /customers/{user_id} — one customer's points, level and cashback on its way

Marketing managers may read the levels: they pick one when making a
members-only coupon. Only a Super Admin changes them, because a level is a
promise to every customer at once.
"""
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from loguru import logger
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.database import get_db
from app.core.dependencies import require_roles
from app.models.coupon import Coupon
from app.models.loyalty import LoyaltyAccount, LoyaltyTier
from app.models.user import User
from app.schemas.loyalty import LoyaltyTierCreate, LoyaltyTierUpdate
from app.services.loyalty_service import LoyaltyService
from app.services.order_reward_service import OrderRewardService

router = APIRouter()

CanRead = Depends(require_roles("super_admin", "marketing_manager"))
SuperAdmin = Depends(require_roles("super_admin"))

_FIELDS = (
    "name", "name_si", "name_ta", "min_points", "color", "is_active",
    "reward_discount_type", "reward_discount_value", "reward_min_order_value",
    "reward_max_discount", "reward_valid_days",
)


def _num(value):
    return float(value) if value is not None else None


def _format(tier: LoyaltyTier, members: int = 0, coupons: int = 0) -> dict:
    return {
        "tier_id": str(tier.tier_id),
        "name": tier.name,
        "name_si": tier.name_si,
        "name_ta": tier.name_ta,
        "min_points": tier.min_points,
        "color": tier.color,
        "is_active": tier.is_active,
        "reward_discount_type": tier.reward_discount_type,
        "reward_discount_value": _num(tier.reward_discount_value),
        "reward_min_order_value": _num(tier.reward_min_order_value),
        "reward_max_discount": _num(tier.reward_max_discount),
        "reward_valid_days": tier.reward_valid_days,
        # Customers whose points currently put them on this level.
        "members": members,
        # Members-only coupons that require it; a level they need cannot go.
        "coupons": coupons,
    }


def _bad_request(message: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=message)


async def _get_or_404(db: AsyncSession, tier_id: UUID) -> LoyaltyTier:
    tier = await db.get(LoyaltyTier, tier_id)
    if tier is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Level not found")
    return tier


async def _min_points_taken(db: AsyncSession, min_points: int, exclude: UUID = None) -> bool:
    query = select(LoyaltyTier.tier_id).where(LoyaltyTier.min_points == min_points)
    if exclude is not None:
        query = query.where(LoyaltyTier.tier_id != exclude)
    return (await db.scalar(query)) is not None


@router.get("/tiers", response_model=dict, dependencies=[CanRead])
async def list_tiers(db: AsyncSession = Depends(get_db)):
    tiers = list(
        (await db.execute(select(LoyaltyTier).order_by(LoyaltyTier.min_points))).scalars().all()
    )
    coupon_counts = dict(
        (
            await db.execute(
                select(Coupon.min_tier_id, func.count(Coupon.coupon_id))
                .where(Coupon.min_tier_id.isnot(None))
                .group_by(Coupon.min_tier_id)
            )
        ).all()
    )

    # Members per level: the band from this level's threshold to the next
    # active one's. Inactive levels hold nobody -- nobody is placed on them.
    active = [t for t in tiers if t.is_active]
    members: dict = {}
    for i, tier in enumerate(active):
        upper = active[i + 1].min_points if i + 1 < len(active) else None
        query = select(func.count()).select_from(LoyaltyAccount).where(
            LoyaltyAccount.lifetime_points >= tier.min_points
        )
        if upper is not None:
            query = query.where(LoyaltyAccount.lifetime_points < upper)
        members[tier.tier_id] = await db.scalar(query) or 0

    return {
        "success": True,
        "data": [
            _format(t, members.get(t.tier_id, 0), coupon_counts.get(t.tier_id, 0))
            for t in tiers
        ],
    }


@router.post("/tiers", response_model=dict, status_code=status.HTTP_201_CREATED)
async def create_tier(
    data: LoyaltyTierCreate,
    db: AsyncSession = Depends(get_db),
    admin=SuperAdmin,
):
    if await _min_points_taken(db, data.min_points):
        raise _bad_request("Another level already starts at that many points")

    tier = LoyaltyTier(**data.model_dump())
    db.add(tier)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise _bad_request("Another level already starts at that many points")
    await db.refresh(tier)
    logger.info(f"[admin] Loyalty level {tier.name} @{tier.min_points} created by {admin.email}")
    return {"success": True, "data": _format(tier)}


@router.put("/tiers/{tier_id}", response_model=dict)
async def update_tier(
    tier_id: UUID,
    data: LoyaltyTierUpdate,
    db: AsyncSession = Depends(get_db),
    admin=SuperAdmin,
):
    tier = await _get_or_404(db, tier_id)
    changes = data.model_dump(exclude_unset=True)

    # Check the level as it will be, not the fragment that was sent.
    merged = {field: getattr(tier, field) for field in _FIELDS}
    merged.update(changes)
    if merged.get("reward_discount_type") is None:
        # No reward: the reward terms have nothing to describe.
        for field in ("reward_discount_value", "reward_max_discount", "reward_valid_days"):
            merged[field] = None
            changes[field] = None
    if merged.get("reward_min_order_value") is None:
        merged["reward_min_order_value"] = 0
        changes["reward_min_order_value"] = 0
    try:
        LoyaltyTierCreate.model_validate(merged)
    except ValidationError as exc:
        raise _bad_request(exc.errors()[0].get("msg", "Invalid level"))

    if "min_points" in changes and await _min_points_taken(db, changes["min_points"], exclude=tier_id):
        raise _bad_request("Another level already starts at that many points")

    for field, value in changes.items():
        setattr(tier, field, value)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise _bad_request("Another level already starts at that many points")
    await db.refresh(tier)
    logger.info(f"[admin] Loyalty level {tier.name} updated by {admin.email}: {sorted(changes)}")
    return {"success": True, "data": _format(tier)}


@router.delete("/tiers/{tier_id}", response_model=dict)
async def delete_tier(
    tier_id: UUID,
    db: AsyncSession = Depends(get_db),
    admin=SuperAdmin,
):
    tier = await _get_or_404(db, tier_id)
    in_use = await db.scalar(
        select(func.count(Coupon.coupon_id)).where(Coupon.min_tier_id == tier_id)
    )
    if in_use:
        # Deleting it would have to either open those coupons to everyone or
        # delete them; neither is a decision to make by accident.
        raise _bad_request(
            f"{in_use} coupon(s) are for this level's members. Switch the level "
            "off instead, or change those coupons first."
        )
    await db.delete(tier)
    await db.commit()
    logger.info(f"[admin] Loyalty level {tier.name} deleted by {admin.email}")
    return {"success": True, "data": None, "message": "Level deleted"}


@router.get("/customers/{user_id}", response_model=dict, dependencies=[CanRead])
async def customer_loyalty(user_id: UUID, db: AsyncSession = Depends(get_db)):
    """What support needs to answer "where are my points": the lot, in one call."""
    if await db.get(User, user_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Customer not found")
    pending = await OrderRewardService.pending_for_user(db, user_id)
    return {
        "success": True,
        "data": {
            **await LoyaltyService.summary(db, user_id),
            "pending_cashback": float(OrderRewardService.pending_total(pending)),
            "pending": pending,
        },
    }
