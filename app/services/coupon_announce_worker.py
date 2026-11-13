"""
"A coupon for you" -- the push an admin turns on with a coupon's
"Notify customers" switch (owner, 2026-11-13; docs/plans/coupon-announce.md).

When: once the coupon has started (valid_from), while it is active, in date and
has budget left. Who:

* a Specific Customers coupon -- the accounts on its list, plus anyone who
  holds a listed number (someone who signed up with it later). Swept for as
  long as the coupon is live, so customers added later get theirs;
* a Public "All customers" coupon -- the Push Campaigns audience: everyone
  for a network coupon, the customers a branch has served for a branch one;
* a Public members-only coupon -- that audience, from the level up.

"All" and members-only coupons are announced once (``coupons.announced_at``).
A hidden-code coupon for everyone is never announced -- the admin endpoint
refuses the switch.

A marketing push (the Offers switch); never to a closed, blocked or inactive
account, nor to someone who cannot use the coupon (limit reached, or a
first-order coupon for someone who has ordered). The ``coupon_announcements``
primary key is the dedupe: the row is written before the push, so a retried
sweep or a second API worker never sends twice.
"""
import asyncio
from datetime import datetime, timezone

from loguru import logger
from sqlalchemy import func, or_, select
from sqlalchemy.dialects.postgresql import insert

from app.config.database import async_session_maker
from app.models.coupon import Coupon, CouponAnnouncement, CouponWhitelistEntry
from app.models.loyalty import LoyaltyAccount, LoyaltyTier
from app.models.notification import NotificationType
from app.models.user import User
from app.services import notification_i18n
from app.services.campaign_service import audience_filter
from app.services.coupon_service import CouponService
from app.services.notification_service import NotificationService

SWEEP_SECONDS = 300


def can_announce(coupon: Coupon) -> bool:
    """A hidden code is only ever sent to customers it was made for."""
    return bool(coupon.is_public) or (coupon.target_customer_type or "all") == "specific"


async def due_coupons(db, now: datetime) -> list[Coupon]:
    rows = await db.execute(
        select(Coupon).where(
            Coupon.notify_customers.is_(True),
            Coupon.is_active.is_(True),
            Coupon.valid_from <= now,
            Coupon.valid_until > now,
            or_(Coupon.target_customer_type == "specific", Coupon.announced_at.is_(None)),
        )
    )
    return [c for c in rows.scalars().all() if not c.is_budget_depleted and can_announce(c)]


async def audience(db, coupon: Coupon) -> list:
    """User ids the coupon is for, before the per-customer checks."""
    live = audience_filter("offers", None)
    if (coupon.target_customer_type or "all") == "specific":
        entries = (
            await db.execute(
                select(CouponWhitelistEntry.user_id, CouponWhitelistEntry.phone).where(
                    CouponWhitelistEntry.coupon_id == coupon.coupon_id
                )
            )
        ).all()
        ids = {uid for uid, _ in entries if uid is not None}
        shapes = set()
        for _, phone in entries:
            if phone and phone.startswith("+94"):
                local = phone[3:]
                shapes.update({phone, f"0{local}", f"94{local}", local})
        match = []
        if ids:
            match.append(User.user_id.in_(list(ids)))
        if shapes:
            match.append(User.phone.in_(list(shapes)))
        if not match:
            return []
        return list((await db.execute(select(User.user_id).where(live, or_(*match)))).scalars())

    stmt = select(User.user_id).where(audience_filter("offers", coupon.branch_id))
    if coupon.min_tier_id is not None:
        tier = await db.get(LoyaltyTier, coupon.min_tier_id)
        if tier is None:
            return []
        # Outer: a level from 0 points includes customers with no points row.
        stmt = stmt.outerjoin(LoyaltyAccount, LoyaltyAccount.user_id == User.user_id).where(
            func.coalesce(LoyaltyAccount.lifetime_points, 0) >= tier.min_points
        )
    return list((await db.execute(stmt)).scalars())


async def _claim(db, coupon_id, user_id) -> bool:
    """Write the announcement row; False when someone already sent this one."""
    result = await db.execute(
        insert(CouponAnnouncement)
        .values(coupon_id=coupon_id, user_id=user_id)
        .on_conflict_do_nothing()
        .returning(CouponAnnouncement.user_id)
    )
    claimed = result.first() is not None
    await db.commit()
    return claimed


async def _can_use(db, coupon: Coupon, user_id) -> bool:
    if coupon.per_user_limit:
        used = await CouponService.redemptions_by_user(db, coupon.code, user_id)
        if used >= coupon.per_user_limit:
            return False
    if coupon.first_order_only and await CouponService.has_completed_order(
        db, user_id, coupon.branch_id
    ):
        return False
    return True


async def announce(db, now: datetime) -> int:
    sent = 0
    for coupon in await due_coupons(db, now):
        already = set(
            (
                await db.execute(
                    select(CouponAnnouncement.user_id).where(
                        CouponAnnouncement.coupon_id == coupon.coupon_id
                    )
                )
            ).scalars()
        )
        for user_id in await audience(db, coupon):
            if user_id in already or not await _can_use(db, coupon, user_id):
                continue
            if not await _claim(db, coupon.coupon_id, user_id):
                continue
            await NotificationService.notify(
                db,
                user_id,
                template="coupon.announce",
                type=NotificationType.PROMOTION,
                params={"code": coupon.code},
                per_lang_params=lambda lang, c=coupon: {
                    "off": notification_i18n.coupon_value(c.discount_type, c.discount_value, lang)
                },
                category="offers",
                reference_type="coupon",
                reference_id=coupon.coupon_id,
                data={"screen": "coupons", "coupon_code": coupon.code},
            )
            sent += 1
        if (coupon.target_customer_type or "all") != "specific":
            coupon.announced_at = now
            await db.commit()
    return sent


async def run_coupon_announce_worker():
    while True:
        try:
            async with async_session_maker() as db:
                count = await announce(db, datetime.now(timezone.utc))
                if count:
                    logger.info(f"Coupon announcements sent: {count}")
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Coupon announcement sweep failed; check migration 116")
        await asyncio.sleep(SWEEP_SECONDS)
