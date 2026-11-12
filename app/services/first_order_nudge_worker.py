"""
"Place your first order" -- one push per new customer (owner, 2026-11-12).

docs/plans/first-order-nudge.md holds the owner's rules. In short:

* Who: an active, unblocked, undeleted customer with no order at all, who signed
  up after this feature first ran (``first_order_nudge_state.since``) and has
  the Offers switch on. With or without an address.
* When: 48 hours after sign-up, at the next 18:30 in Colombo. Sweeps run
  between 18:30 and 21:00 only, so a push never arrives at night; a sweep
  missed by a restart catches up the next evening.
* Once, ever: the ``first_order_nudges`` row is written before the push.
* Copy: the customer's name and area, and free delivery only while the area's
  branch offer is live, with its minimum and end date read at send time. A
  customer with no served address gets an invitation that promises nothing.
* Tap: the app opens the Offers page (``screen = offers``).
"""

import asyncio
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Optional, Tuple

from loguru import logger
from sqlalchemy import exists, select
from sqlalchemy.dialects.postgresql import insert

from app.config.database import async_session_maker
from app.config.settings import settings
from app.models.first_order_nudge import FirstOrderNudge, FirstOrderNudgeState
from app.models.notification import NotificationType
from app.models.order import Order
from app.models.user import Address, User
from app.services import branch_service, free_delivery_service
from app.services.notification_service import NotificationService

COLOMBO = timezone(timedelta(hours=5, minutes=30))
WAIT = timedelta(hours=48)
SEND_FROM = time(18, 30)
SEND_UNTIL = time(21, 0)
SWEEP_SECONDS = 300
BATCH = 200

_MONTHS = {
    "en": ["January", "February", "March", "April", "May", "June", "July",
           "August", "September", "October", "November", "December"],
    "si": ["ජනවාරි", "පෙබරවාරි", "මාර්තු", "අප්‍රේල්", "මැයි", "ජූනි", "ජූලි",
           "අගෝස්තු", "සැප්තැම්බර්", "ඔක්තෝබර්", "නොවැම්බර්", "දෙසැම්බර්"],
    "ta": ["ஜனவரி", "பிப்ரவரி", "மார்ச்", "ஏப்ரல்", "மே", "ஜூன்", "ஜூலை",
           "ஆகஸ்ட்", "செப்டம்பர்", "அக்டோபர்", "நவம்பர்", "டிசம்பர்"],
}
# "Hi {name}!" for a customer with no name on the account.
_NO_NAME = {"en": "there", "si": "යාළුවා", "ta": "நண்பரே"}


def in_send_window(now: datetime) -> bool:
    local = now.astimezone(COLOMBO).time()
    return SEND_FROM <= local < SEND_UNTIL


def colombo_date(moment: datetime, lang: str) -> str:
    """'2 December' / 'දෙසැම්බර් 2' / 'டிசம்பர் 2', in Colombo time."""
    local = moment.astimezone(COLOMBO)
    month = _MONTHS.get(lang, _MONTHS["en"])[local.month - 1]
    return f"{local.day} {month}" if lang == "en" else f"{month} {local.day}"


def rupees(amount: Decimal) -> str:
    """1000 -> '1,000'; 1250.5 -> '1,250.50'."""
    value = Decimal(amount)
    return f"{value:,.0f}" if value == value.to_integral_value() else f"{value:,.2f}"


def first_name(user: User) -> Optional[str]:
    raw = (user.first_name or user.full_name or "").strip()
    if not raw:
        return None
    word = raw.split()[0]
    return word[:1].upper() + word[1:]


async def ensure_since(db, now: datetime) -> datetime:
    """The moment the feature first ran; written once, then only read."""
    await db.execute(
        insert(FirstOrderNudgeState).values(id=1, since=now).on_conflict_do_nothing()
    )
    await db.commit()
    return await db.scalar(select(FirstOrderNudgeState.since).where(FirstOrderNudgeState.id == 1))


async def due_users(db, now: datetime, since: datetime) -> list[User]:
    # users.created_at is a naive UTC column.
    signed_up_before = (now - WAIT).astimezone(timezone.utc).replace(tzinfo=None)
    signed_up_after = since.astimezone(timezone.utc).replace(tzinfo=None)
    rows = await db.execute(
        select(User)
        .where(
            User.role == "customer",
            User.is_active.is_(True),
            User.is_blocked.isnot(True),
            User.deleted_at.is_(None),
            User.notify_offers.isnot(False),
            User.created_at >= signed_up_after,
            User.created_at <= signed_up_before,
            ~exists().where(Order.user_id == User.user_id),
            ~exists().where(FirstOrderNudge.user_id == User.user_id),
        )
        .order_by(User.created_at)
        .limit(BATCH)
    )
    return list(rows.scalars().all())


async def _claim(db, user_id, variant: str) -> bool:
    """Write the row first; False when another worker already took this one."""
    result = await db.execute(
        insert(FirstOrderNudge)
        .values(user_id=user_id, variant=variant)
        .on_conflict_do_nothing()
        .returning(FirstOrderNudge.user_id)
    )
    claimed = result.first() is not None
    await db.commit()
    return claimed


async def _served_area(db, user_id) -> Tuple[Optional[str], Optional[object]]:
    """(postal city, branch id) of the customer's default address, else the newest
    one, when a branch serves it; (None, None) otherwise."""
    address = await db.scalar(
        select(Address)
        .where(Address.user_id == user_id)
        .order_by(Address.is_default.desc(), Address.created_at.desc())
        .limit(1)
    )
    if address is None or not address.postal_city:
        return None, None
    mapping = await branch_service.resolve_branch_by_postal_city(db, address.postal_city)
    if mapping is None:
        return None, None
    return address.postal_city.strip(), mapping.branch_id


async def compose(db, user: User) -> Tuple[str, dict, object]:
    """(template, params, per_lang_params) for this customer, as of now."""
    area, branch_id = await _served_area(db, user.user_id)
    name = first_name(user)
    offer = await free_delivery_service.live_offer(db, branch_id) if branch_id else None

    params: dict = {"area": area or ""}
    ends_at = free_delivery_service.offer_ends_at(offer) if offer is not None else None

    def per_lang(lang: str) -> dict:
        extra = {"name": name or _NO_NAME[lang]}
        if ends_at is not None:
            extra["end"] = colombo_date(ends_at, lang)
        return extra

    if area is None:
        return "nudge.first_order.general", params, per_lang
    if offer is None or ends_at is None:
        return "nudge.first_order.area", params, per_lang
    if offer.min_order and Decimal(offer.min_order) > 0:
        params["min"] = rupees(offer.min_order)
        return "nudge.first_order.offer_min", params, per_lang
    return "nudge.first_order.offer", params, per_lang


async def nudge(db, now: datetime) -> int:
    """One sweep. Returns how many customers were nudged."""
    if not in_send_window(now):
        return 0
    since = await ensure_since(db, now)
    sent = 0
    for user in await due_users(db, now, since):
        template, params, per_lang = await compose(db, user)
        variant = template.rsplit(".", 1)[1]
        if not await _claim(db, user.user_id, variant):
            continue
        await NotificationService.notify(
            db,
            user.user_id,
            template=template,
            type=NotificationType.PROMOTION,
            params=params,
            per_lang_params=per_lang,
            category="offers",
            data={"screen": "offers", "type": "promotion"},
        )
        sent += 1
    return sent


async def run_first_order_nudge_worker():
    if not settings.first_order_nudge_enabled:
        return
    while True:
        try:
            async with async_session_maker() as db:
                count = await nudge(db, datetime.now(timezone.utc))
                if count:
                    logger.info("First-order nudge sent to {} customer(s)", count)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("First-order nudge sweep failed ({})", type(exc).__name__)
        await asyncio.sleep(SWEEP_SECONDS)
