"""
SRIBEESonline — loyalty points and levels.

A customer earns points on the value of what they bought and kept: the order
total without delivery, at the Super Admin's "rupees per point" rate. Points
are added only when an order's return window closes (see OrderRewardService),
and they never go down. The level a customer is at is read from their points
against the ``loyalty_tiers`` table every time, so an admin editing thresholds
takes effect at once without rewriting anyone's points.

Rupees short of the next point carry over (``spend_carry``). Rounding each
order down on its own would make three Rs 700 orders worth nothing at Rs 1,000
a point; carrying makes them worth two.
"""
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import ROUND_DOWN, Decimal
from typing import Optional
from uuid import UUID, uuid4

from loguru import logger
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.coupon import Coupon
from app.models.loyalty import LoyaltyAccount, LoyaltyTier, LoyaltyTierReward
from app.services.app_settings_service import AppSettingsService
from app.utils.i18n import DEFAULT_LANGUAGE, localized


def _money(value: Decimal) -> Decimal:
    return Decimal(value).quantize(Decimal("0.01"))


@dataclass
class PointsResult:
    points_added: int
    lifetime_points: int
    # Levels crossed by this addition, lowest first.
    tiers_reached: list = field(default_factory=list)


@dataclass
class IssuedReward:
    tier: LoyaltyTier
    coupon: Coupon


class LoyaltyService:
    """Points, levels and level rewards."""

    # ------------------------------------------------------------------
    # Levels
    # ------------------------------------------------------------------

    @staticmethod
    async def active_tiers(db: AsyncSession) -> list[LoyaltyTier]:
        result = await db.execute(
            select(LoyaltyTier)
            .where(LoyaltyTier.is_active.is_(True))
            .order_by(LoyaltyTier.min_points)
        )
        return list(result.scalars().all())

    @staticmethod
    def tier_for(points: int, tiers: list[LoyaltyTier]) -> Optional[LoyaltyTier]:
        """The highest level these points reach, or None below the lowest."""
        current = None
        for tier in sorted(tiers, key=lambda t: t.min_points):
            if tier.min_points <= points:
                current = tier
        return current

    @staticmethod
    def next_tier(points: int, tiers: list[LoyaltyTier]) -> Optional[LoyaltyTier]:
        for tier in sorted(tiers, key=lambda t: t.min_points):
            if tier.min_points > points:
                return tier
        return None

    # ------------------------------------------------------------------
    # Points
    # ------------------------------------------------------------------

    @staticmethod
    async def get_points(db: AsyncSession, user_id: UUID) -> int:
        points = await db.scalar(
            select(LoyaltyAccount.lifetime_points).where(LoyaltyAccount.user_id == user_id)
        )
        return int(points or 0)

    @staticmethod
    def points_for_spend(
        carry: Decimal, amount: Decimal, spend_per_point: Decimal
    ) -> tuple[int, Decimal]:
        """
        Points for ``amount`` on top of the rupees already carried, and the new
        carry. Pure, so the arithmetic is tested without a database.
        """
        if spend_per_point <= 0:
            raise ValueError("spend_per_point must be positive")
        total = Decimal(str(carry or 0)) + max(Decimal(str(amount or 0)), Decimal("0"))
        points = int((total / spend_per_point).to_integral_value(rounding=ROUND_DOWN))
        remainder = total - spend_per_point * points
        return points, _money(remainder)

    @staticmethod
    async def _locked_account(db: AsyncSession, user_id: UUID) -> LoyaltyAccount:
        """
        The customer's account, created on first use and locked for this
        transaction. The lock serialises two settlements for the same person,
        so neither can add points onto a total the other is about to replace.
        """
        await db.execute(
            pg_insert(LoyaltyAccount)
            .values(user_id=user_id)
            .on_conflict_do_nothing(index_elements=["user_id"])
        )
        result = await db.execute(
            select(LoyaltyAccount)
            .where(LoyaltyAccount.user_id == user_id)
            .with_for_update()
        )
        return result.scalar_one()

    @staticmethod
    async def add_spend(db: AsyncSession, user_id: UUID, amount: Decimal) -> PointsResult:
        """
        Add a settled order's value. The only thing that ever changes points,
        and it only ever adds. Staged in the caller's transaction.
        """
        amount = max(_money(Decimal(str(amount or 0))), Decimal("0"))
        spend_per_point = Decimal(
            str(await AppSettingsService.get_loyalty_spend_per_point(db))
        )
        account = await LoyaltyService._locked_account(db, user_id)

        before = int(account.lifetime_points or 0)
        added, carry = LoyaltyService.points_for_spend(
            Decimal(str(account.spend_carry or 0)), amount, spend_per_point
        )
        account.lifetime_points = before + added
        account.spend_carry = carry
        account.lifetime_spend = _money(Decimal(str(account.lifetime_spend or 0)) + amount)

        tiers = await LoyaltyService.active_tiers(db)
        reached = [t for t in tiers if before < t.min_points <= account.lifetime_points]
        return PointsResult(added, account.lifetime_points, reached)

    # ------------------------------------------------------------------
    # Level rewards
    # ------------------------------------------------------------------

    @staticmethod
    def _reward_code(tier: LoyaltyTier) -> str:
        """
        Readable and unguessable: GOLD-9F3A1C. The random part is what keeps
        it private; target_user_id is what makes it theirs even if guessed.
        """
        prefix = "".join(ch for ch in (tier.name or "LEVEL").upper() if ch.isalnum())[:10]
        return f"{prefix or 'LEVEL'}-{secrets.token_hex(3).upper()}"

    @staticmethod
    async def issue_level_rewards(
        db: AsyncSession, user_id: UUID, points: int
    ) -> list[IssuedReward]:
        """
        Issue the coupon of every level these points reach that this customer
        has not been given yet.

        "Not given yet" rather than "just crossed": a reward an admin adds to
        an existing level, or a threshold they lower, reaches the customers
        already there at their next settled order, and a settlement that
        failed halfway is made good by the next one. The primary key on
        loyalty_tier_rewards is what makes it once, whoever asks.
        """
        tiers = [
            t for t in await LoyaltyService.active_tiers(db)
            if t.has_reward and t.min_points <= points
        ]
        if not tiers:
            return []

        issued_ids = set(
            (
                await db.execute(
                    select(LoyaltyTierReward.tier_id).where(
                        LoyaltyTierReward.user_id == user_id
                    )
                )
            ).scalars().all()
        )

        now = datetime.now(timezone.utc)
        issued: list[IssuedReward] = []
        for tier in tiers:
            if tier.tier_id in issued_ids:
                continue

            code = LoyaltyService._reward_code(tier)
            while await db.scalar(
                select(Coupon.coupon_id).where(func.lower(Coupon.code) == code.lower())
            ):
                code = LoyaltyService._reward_code(tier)

            coupon = Coupon(
                coupon_id=uuid4(),
                code=code,
                description=f"{tier.name} level reward",
                # Network-wide: the level was earned across every branch.
                branch_id=None,
                discount_type=tier.reward_discount_type,
                discount_value=tier.reward_discount_value,
                min_order_value=tier.reward_min_order_value or 0,
                max_discount_amount=tier.reward_max_discount,
                valid_from=now,
                valid_until=now + timedelta(days=int(tier.reward_valid_days)),
                is_active=True,
                # One person, one use -- the same shape as an Assign Promo.
                usage_limit=1,
                per_user_limit=1,
                target_user_id=user_id,
                is_public=False,
                exclude_quick_sale=True,
            )
            db.add(coupon)
            db.add(LoyaltyTierReward(user_id=user_id, tier_id=tier.tier_id, coupon_id=coupon.coupon_id))
            issued.append(IssuedReward(tier=tier, coupon=coupon))

        if issued:
            await db.flush()
            logger.info(
                f"Level reward(s) {[r.coupon.code for r in issued]} issued to {user_id}"
            )
        return issued

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    @staticmethod
    def tier_json(tier: Optional[LoyaltyTier], lang: str = DEFAULT_LANGUAGE) -> Optional[dict]:
        if tier is None:
            return None
        return {
            "tier_id": str(tier.tier_id),
            "name": localized(tier, "name", lang),
            "min_points": tier.min_points,
            "color": tier.color,
        }

    @staticmethod
    async def summary(
        db: AsyncSession, user_id: UUID, lang: str = DEFAULT_LANGUAGE
    ) -> dict:
        """Everything the app draws: points, level, the next level, the ladder."""
        account = await db.get(LoyaltyAccount, user_id)
        points = int(account.lifetime_points) if account else 0
        carry = Decimal(str(account.spend_carry)) if account else Decimal("0")
        spend_per_point = Decimal(
            str(await AppSettingsService.get_loyalty_spend_per_point(db))
        )

        tiers = await LoyaltyService.active_tiers(db)
        current = LoyaltyService.tier_for(points, tiers)
        upcoming = LoyaltyService.next_tier(points, tiers)

        next_json = LoyaltyService.tier_json(upcoming, lang)
        if next_json is not None:
            next_json["points_needed"] = upcoming.min_points - points

        return {
            "points": points,
            "tier": LoyaltyService.tier_json(current, lang),
            "next_tier": next_json,
            "tiers": [LoyaltyService.tier_json(t, lang) for t in tiers],
            "spend_per_point": float(spend_per_point),
            # How far this customer is from their next point, in rupees.
            "spend_to_next_point": float(_money(max(spend_per_point - carry, Decimal("0")))),
        }
