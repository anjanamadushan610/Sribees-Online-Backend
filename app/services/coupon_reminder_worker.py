"""
"Your coupon ends soon" -- one push per customer per coupon (owner, 2026-11-10).

Who: customers who collected the coupon (the Redis wallet set) or were issued
it (``target_user_id``), and have not used it. When: once the coupon has
``REMIND_BEFORE`` or less left. A marketing push (the Offers switch), so a
customer who turned offers off hears nothing.

The ``coupon_expiry_reminders`` primary key is the dedupe: the row is written
before the push, so a retried sweep or a second API worker never sends twice.
"""
import asyncio
import math
from datetime import datetime, timedelta, timezone

from loguru import logger
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from app.config.database import async_session_maker
from app.config.redis import get_redis_client
from app.models.coupon import Coupon, CouponExpiryReminder
from app.models.notification import NotificationType
from app.models.user import User
from app.services import notification_i18n
from app.services.coupon_service import CouponService
from app.services.notification_service import NotificationService

REMIND_BEFORE = timedelta(hours=3)
SWEEP_SECONDS = 300


async def _collectors(redis, code: str) -> set[str]:
    """User ids whose wallet set holds ``code``."""
    if redis is None:
        return set()
    found: set[str] = set()
    async for key in redis.scan_iter(match="user:*:collected_coupons", count=500):
        key = key.decode() if isinstance(key, bytes) else key
        if await redis.sismember(key, code.upper()):
            found.add(key.split(":")[1])
    return found


async def due_coupons(db, now: datetime) -> list[Coupon]:
    rows = await db.execute(
        select(Coupon).where(
            Coupon.is_active.is_(True),
            Coupon.valid_until > now,
            Coupon.valid_until <= now + REMIND_BEFORE,
            Coupon.valid_from <= now,
        )
    )
    return [c for c in rows.scalars().all() if not c.is_budget_depleted]


async def _claim(db, coupon_id, user_id) -> bool:
    """Write the reminder row; False when someone already sent this one."""
    result = await db.execute(
        insert(CouponExpiryReminder)
        .values(coupon_id=coupon_id, user_id=user_id)
        .on_conflict_do_nothing()
        .returning(CouponExpiryReminder.user_id)
    )
    claimed = result.first() is not None
    await db.commit()
    return claimed


async def remind(db, redis, now: datetime) -> int:
    sent = 0
    for coupon in await due_coupons(db, now):
        users = await _collectors(redis, coupon.code)
        if coupon.target_user_id is not None:
            users.add(str(coupon.target_user_id))
        already = {
            str(u)
            for u in (
                await db.execute(
                    select(CouponExpiryReminder.user_id).where(
                        CouponExpiryReminder.coupon_id == coupon.coupon_id
                    )
                )
            ).scalars()
        }
        hours = max(1, math.ceil((coupon.valid_until - now).total_seconds() / 3600))
        for user_id in sorted(users - already):
            user = await db.get(User, user_id)
            if user is None or not user.is_active or getattr(user, "is_blocked", False):
                continue
            if coupon.target_user_id is not None and coupon.target_user_id != user.user_id:
                continue
            if await CouponService.redemptions_by_user(db, coupon.code, user.user_id):
                continue
            if not await _claim(db, coupon.coupon_id, user.user_id):
                continue
            await NotificationService.notify(
                db,
                user.user_id,
                template="coupon.expiring",
                type=NotificationType.PROMOTION,
                params={"code": coupon.code, "hours": hours},
                per_lang_params=lambda lang, c=coupon: {
                    "off": notification_i18n.coupon_value(c.discount_type, c.discount_value, lang)
                },
                category="offers",
                reference_type="coupon",
                reference_id=coupon.coupon_id,
                data={"screen": "coupons", "coupon_code": coupon.code},
            )
            sent += 1
    return sent


async def run_coupon_reminder_worker():
    while True:
        try:
            async with async_session_maker() as db:
                count = await remind(db, get_redis_client(), datetime.now(timezone.utc))
                if count:
                    logger.info(f"Coupon expiry reminders sent: {count}")
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Coupon reminder sweep failed; check migration 107")
        await asyncio.sleep(SWEEP_SECONDS)
