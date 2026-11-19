"""
SRIBEESonline - customer activity log (owner, 2026-11-19).

The customer app and the web store send what a shopper does, in batches:
screens with time on screen and scroll depth, products opened, image swipes
and zooms, searches, categories, cart and wishlist changes, banner and offer
taps, checkout steps. Guests too -- the batch names its install, and once
that install signs in, its guest events show on the customer's timeline.

Rules (docs/plans/customer-activity-log.md):
- Only known event names; props are small flat values, never free blobs.
- The customer comes from the token, never the body.
- Never passwords, OTPs, card data, typed addresses or chat text.
- Kept forever; deleted with a closed account (account_deletion_service).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import and_, or_, select, tuple_, union
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.activity import ActivityEvent
from app.models.app_usage import AppInstall
from app.models.category import Category
from app.models.product import Product

LK_TZ = ZoneInfo("Asia/Colombo")

MAX_EVENTS_PER_BATCH = 100
MAX_PROPS = 12
MAX_PROP_TEXT = 200
# A phone's clock can be wrong; an offline queue can be a few days old.
OLDEST_BELIEVABLE = timedelta(days=14)
FUTURE_SLACK = timedelta(minutes=10)
TIMELINE_PAGE_MAX = 500

# Every event the app or web may send. Anything else is refused, so a typo in
# a client shows up as a test failure, not as rows nobody can read.
EVENTS: frozenset[str] = frozenset(
    {
        "app_open",          # launch or resume
        "app_background",    # props: seconds (on screen since open)
        "screen",            # sent when a screen is left: screen, props: seconds, scroll_pct, items_seen
        "product_view",      # product_id, props: source (home, search, category, banner, share_link, ...)
        "image_swipe",       # product_id, props: index, total
        "image_zoom",        # product_id, props: index
        "search",            # props: query, results
        "category_open",     # category_id, props: level (category|subcategory)
        "add_to_cart",       # product_id, props: qty, variant_id, price
        "remove_from_cart",  # product_id, props: qty
        "cart_qty",          # product_id, props: from, to
        "wishlist_add",      # product_id
        "wishlist_remove",   # product_id
        "banner_tap",        # props: banner_id, link_type
        "offer_tap",         # props: coupon_id / offer kind
        "coupon_collect",    # props: coupon_id, code
        "checkout_step",     # props: step (cart|address|slot|payment|placed), order_id
        "share",             # product_id, props: channel
        "notification_open", # props: type
        "sign_in",           # props: method
        "sign_up",           # props: method
        "sign_out",
        "tap",               # a named button that has no event of its own: props: target
    }
)

# Never stored even if a client sends them by mistake.
_FORBIDDEN_KEYS = frozenset({"password", "otp", "pin", "card", "cvv", "token", "address", "message", "nic"})


class ActivityRejected(ValueError):
    """The batch broke a rule; the API answers 422 with this text."""


def _clean_props(props: Optional[dict]) -> dict:
    if not props:
        return {}
    if not isinstance(props, dict) or len(props) > MAX_PROPS:
        raise ActivityRejected(f"props: at most {MAX_PROPS} flat keys")
    out: dict[str, Any] = {}
    for key, value in props.items():
        if not isinstance(key, str) or not key.isidentifier() or len(key) > 30:
            raise ActivityRejected(f"props key {key!r} is not a short name")
        if key.lower() in _FORBIDDEN_KEYS:
            continue
        if value is None or isinstance(value, bool):
            out[key] = value
        elif isinstance(value, (int, float)):
            if abs(value) > 1e12:
                raise ActivityRejected(f"props.{key} is out of range")
            out[key] = value
        elif isinstance(value, str):
            out[key] = value[:MAX_PROP_TEXT]
        else:
            raise ActivityRejected(f"props.{key} must be text, a number or true/false")
    return out


def _clamp_time(at: Optional[datetime], now: datetime) -> datetime:
    if at is None:
        return now
    if at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    if at > now + FUTURE_SLACK:
        return now
    if at < now - OLDEST_BELIEVABLE:
        return now - OLDEST_BELIEVABLE
    return at


async def record_batch(
    db: AsyncSession,
    *,
    device_id: str,
    user_id: Optional[UUID],
    platform: str,
    app_version: Optional[str],
    session_id: Optional[str],
    events: Iterable[dict],
    now: Optional[datetime] = None,
) -> int:
    """Validate and store a batch. All or nothing. Commits. Returns rows written."""
    now = now or datetime.now(timezone.utc)
    rows = []
    for i, e in enumerate(events):
        if i >= MAX_EVENTS_PER_BATCH:
            raise ActivityRejected(f"at most {MAX_EVENTS_PER_BATCH} events per batch")
        name = e.get("event")
        if name not in EVENTS:
            raise ActivityRejected(f"unknown event {name!r}")
        rows.append(
            {
                "device_id": device_id,
                "user_id": user_id,
                "platform": platform,
                "app_version": app_version,
                "session_id": session_id,
                "event": name,
                "screen": e.get("screen"),
                "product_id": e.get("product_id"),
                "category_id": e.get("category_id"),
                "props": _clean_props(e.get("props")),
                "occurred_at": _clamp_time(e.get("occurred_at"), now),
                "received_at": now,
            }
        )
    if rows:
        await db.execute(pg_insert(ActivityEvent).values(rows))
        await db.commit()
    return len(rows)


def _devices_of(user_id: UUID):
    """Every install this customer has used: from the log and from the heartbeat."""
    return union(
        select(ActivityEvent.device_id).where(ActivityEvent.user_id == user_id),
        select(AppInstall.device_id).where(AppInstall.user_id == user_id),
    ).subquery()


def _row(e: ActivityEvent, product_name: Optional[str], category_name: Optional[str]) -> dict:
    return {
        "id": e.id,
        "event": e.event,
        "screen": e.screen,
        "product_id": str(e.product_id) if e.product_id else None,
        "product_name": product_name,
        "category_id": str(e.category_id) if e.category_id else None,
        "category_name": category_name,
        "props": e.props or {},
        "device_id": e.device_id,
        "platform": e.platform,
        "app_version": e.app_version,
        "session_id": e.session_id,
        "is_guest": e.user_id is None,
        "occurred_at": e.occurred_at.isoformat(),
        "occurred_at_lk": e.occurred_at.astimezone(LK_TZ).strftime("%Y-%m-%d %H:%M:%S"),
    }


async def timeline(
    db: AsyncSession,
    *,
    user_id: Optional[UUID] = None,
    device_id: Optional[str] = None,
    before_id: Optional[int] = None,
    limit: int = 200,
) -> dict:
    """
    Newest first. For a customer: their own events plus the guest events of
    every install they signed in on. For an install: everything it sent.
    Page with ``before_id`` (the last id of the previous page).
    """
    if (user_id is None) == (device_id is None):
        raise ValueError("give exactly one of user_id or device_id")
    limit = max(1, min(limit, TIMELINE_PAGE_MAX))

    if user_id is not None:
        devices = _devices_of(user_id)
        who = or_(
            ActivityEvent.user_id == user_id,
            and_(ActivityEvent.user_id.is_(None), ActivityEvent.device_id.in_(select(devices.c.device_id))),
        )
    else:
        who = ActivityEvent.device_id == device_id

    q = (
        select(ActivityEvent, Product.name, Category.name)
        .outerjoin(Product, Product.product_id == ActivityEvent.product_id)
        .outerjoin(Category, Category.category_id == ActivityEvent.category_id)
        .where(who)
    )
    if before_id is not None:
        anchor = await db.get(ActivityEvent, before_id)
        if anchor is not None:
            q = q.where(
                tuple_(ActivityEvent.occurred_at, ActivityEvent.id) < tuple_(anchor.occurred_at, anchor.id)
            )
    q = q.order_by(ActivityEvent.occurred_at.desc(), ActivityEvent.id.desc()).limit(limit + 1)

    result = (await db.execute(q)).all()
    more = len(result) > limit
    events = [_row(e, pn, cn) for e, pn, cn in result[:limit]]
    return {"events": events, "has_more": more, "next_before_id": events[-1]["id"] if more else None}
