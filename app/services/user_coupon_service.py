"""
User Coupon Service — the customer-facing "collect a coupon" flow and the
per-customer redemption limit it backs.

`CouponService` stays the authority on a coupon's *own* rules (validity window,
global usage_limit, discount maths). This service adds the per-user layer:

  * public coupons must be **collected** (a `user_coupons` row) before they can
    be applied at checkout;
  * `coupons.per_user_limit` caps how many times one customer may redeem a code,
    tracked in `user_coupons.times_redeemed`.

Private codes (``is_public = False``) are unaffected unless they set a
``per_user_limit`` — those get a `user_coupons` row created lazily on first
redemption.
"""
from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.coupon import Coupon
from app.models.user_coupon import UserCoupon
from app.services.coupon_service import CouponService, CouponValidationError, _as_aware_utc


def _now() -> datetime:
    return datetime.now(timezone.utc)


class UserCouponService:
    """Per-customer coupon collection and redemption limits."""

    @staticmethod
    async def _get_row(
        db: AsyncSession, user_id: UUID, coupon_id: UUID
    ) -> Optional[UserCoupon]:
        return (
            await db.execute(
                select(UserCoupon).where(
                    UserCoupon.user_id == user_id,
                    UserCoupon.coupon_id == coupon_id,
                )
            )
        ).scalar_one_or_none()

    # ------------------------------------------------------------------ read

    @staticmethod
    def _format(coupon: Coupon, row: Optional[UserCoupon], now: datetime) -> dict:
        collected = row is not None
        times_redeemed = (row.times_redeemed if row else 0) or 0
        limit = coupon.per_user_limit
        expired = now > _as_aware_utc(coupon.valid_until) or not coupon.is_active
        not_started = now < _as_aware_utc(coupon.valid_from)
        per_user_exhausted = limit is not None and times_redeemed >= limit

        if expired:
            state = "expired"
        elif per_user_exhausted:
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
            "discount_value": float(coupon.discount_value or 0),
            "min_order_value": float(coupon.min_order_value or 0),
            "max_discount_amount": (
                float(coupon.max_discount_amount)
                if coupon.max_discount_amount is not None
                else None
            ),
            "valid_from": coupon.valid_from.isoformat() if coupon.valid_from else None,
            "valid_until": coupon.valid_until.isoformat() if coupon.valid_until else None,
            "per_user_limit": limit,
            "times_redeemed": times_redeemed,
            "collected": collected,
            "collected_at": row.collected_at.isoformat() if row and row.collected_at else None,
            # `usable` = can be applied to a cart right now.
            "usable": collected and not expired and not per_user_exhausted and not not_started,
            "state": state,
        }

    @staticmethod
    async def list_available(db: AsyncSession, user_id: UUID) -> list[dict]:
        """Public, currently-valid coupons for the Available Offers feed, each
        annotated with whether this customer has collected it. Globally
        exhausted coupons (used_count >= usage_limit) are hidden."""
        now = _now()
        coupons = (
            await db.execute(
                select(Coupon)
                .where(
                    Coupon.is_public.is_(True),
                    Coupon.is_active.is_(True),
                    Coupon.valid_until >= now,
                )
                .order_by(Coupon.valid_until.asc())
            )
        ).scalars().all()

        rows = {
            uc.coupon_id: uc
            for uc in (
                await db.execute(
                    select(UserCoupon).where(UserCoupon.user_id == user_id)
                )
            ).scalars()
        }

        out = []
        for c in coupons:
            if c.usage_limit is not None and (c.used_count or 0) >= c.usage_limit:
                continue
            out.append(UserCouponService._format(c, rows.get(c.coupon_id), now))
        return out

    @staticmethod
    async def list_collected(db: AsyncSession, user_id: UUID) -> list[dict]:
        """The customer's coupon wallet — every coupon they've collected, newest
        first, with a computed state (collected / used / expired)."""
        now = _now()
        pairs = (
            await db.execute(
                select(UserCoupon, Coupon)
                .join(Coupon, Coupon.coupon_id == UserCoupon.coupon_id)
                .where(UserCoupon.user_id == user_id)
                .order_by(UserCoupon.collected_at.desc())
            )
        ).all()
        return [UserCouponService._format(coupon, row, now) for row, coupon in pairs]

    # --------------------------------------------------------------- mutate

    @staticmethod
    async def collect(db: AsyncSession, user_id: UUID, code: str) -> dict:
        """Add a public coupon to the customer's wallet. Idempotent — collecting
        an already-collected coupon just returns it. Commits."""
        coupon = await CouponService.get_by_code(db, code)
        if coupon is None or not coupon.is_public:
            raise CouponValidationError("This offer is not available.")

        now = _now()
        if not coupon.is_active or now > _as_aware_utc(coupon.valid_until):
            raise CouponValidationError("This offer has expired.")
        if coupon.usage_limit is not None and (coupon.used_count or 0) >= coupon.usage_limit:
            raise CouponValidationError("This offer has been fully claimed.")

        await db.execute(
            pg_insert(UserCoupon)
            .values(user_id=user_id, coupon_id=coupon.coupon_id)
            .on_conflict_do_nothing(index_elements=["user_id", "coupon_id"])
        )
        await db.commit()

        row = await UserCouponService._get_row(db, user_id, coupon.coupon_id)
        return UserCouponService._format(coupon, row, now)

    @staticmethod
    async def assert_can_redeem(
        db: AsyncSession, user_id: UUID, coupon: Coupon
    ) -> None:
        """Raise :class:`CouponValidationError` unless *user_id* may redeem
        *coupon* right now (collection requirement + per-user limit). Call
        alongside ``CouponService.validate`` in the cart/checkout path."""
        row = await UserCouponService._get_row(db, user_id, coupon.coupon_id)

        if coupon.is_public and row is None:
            raise CouponValidationError(
                "Collect this offer from the Offers screen before applying it."
            )

        limit = coupon.per_user_limit
        if limit is not None:
            used = (row.times_redeemed if row else 0) or 0
            if used >= limit:
                raise CouponValidationError(
                    "You've already used this coupon."
                    if limit == 1
                    else f"You've reached the limit of {limit} uses for this coupon."
                )

    @staticmethod
    async def record_redemption(
        db: AsyncSession, user_id: UUID, coupon: Coupon, order_id: UUID
    ) -> None:
        """Increment this customer's redemption count for *coupon*, creating the
        `user_coupons` row if needed (private code, never collected). Staged in
        the caller's transaction — does NOT commit."""
        row = await UserCouponService._get_row(db, user_id, coupon.coupon_id)
        if row is None:
            row = UserCoupon(user_id=user_id, coupon_id=coupon.coupon_id, times_redeemed=0)
            db.add(row)
            await db.flush()
        row.times_redeemed = (row.times_redeemed or 0) + 1
        row.last_redeemed_at = _now()
        row.last_redeemed_order_id = order_id

    @staticmethod
    async def revert_redemption(
        db: AsyncSession, user_id: UUID, coupon_id: UUID, order_id: UUID
    ) -> None:
        """Give a redemption back when the order that used it is cancelled.
        Staged in the caller's transaction — does NOT commit. Idempotent-safe
        (never drops below zero)."""
        row = await UserCouponService._get_row(db, user_id, coupon_id)
        if row is not None and (row.times_redeemed or 0) > 0:
            row.times_redeemed -= 1
            if row.last_redeemed_order_id == order_id:
                row.last_redeemed_order_id = None
