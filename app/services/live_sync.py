"""
SRIBEESonline - telling open apps that what they show has changed.

The app used to re-read everything every 5 minutes, changed or not. Now the
server says when something changed, and the app re-reads only that:

  * Every commit that touches something a customer can see is noticed here,
    at one choke point (SQLAlchemy session events), so no write path has to
    remember to announce itself.
  * Each change bumps a version counter in Redis for its group:
        catalog:all        products, images, variants, categories, reviews
        catalog:<branch>   that branch's prices, sale flags, availability
        banners            the home carousel
        offers             coupons (their terms, not their usage counters)
        user:<user_id>     one customer's orders, notifications, wallet, points
    GET /app/sync-state returns the versions that apply to the caller; the app
    compares them with what it last saw. That check is a few bytes, so the
    app's fallback timer (30 minutes) and every return to the app cost next
    to nothing when nothing changed.
  * Catalog and banner changes also send ONE silent FCM message to the
    ``sync`` topic, at most every ``PUSH_DEBOUNCE_SECONDS``: a price list
    imported as 1,500 updates is one push, not 1,500. A customer's own
    changes need no extra push -- the order / wallet notification that
    already goes to them makes the app check.

Stock is deliberately coarse: an order moving stock from 12 to 11 is not
news to anyone, and announcing it would make every open app re-read the
catalog after every order. Only a product going out of stock or coming back
counts. The exact quantity is still checked where it matters -- at the cart
and at checkout -- and appears on the next re-read.
"""
import asyncio
from contextvars import ContextVar
from typing import Iterable, Optional

from loguru import logger
from sqlalchemy import event, inspect
from sqlalchemy.orm import Session

PUSH_TOPIC = "sync"
PUSH_TYPE = "sync"
# How long changes are gathered before one push goes out.
PUSH_DEBOUNCE_SECONDS = 45

_VERSION_KEY = "sync:v:{}"
_PENDING_KEY = "sync:push:pending"
_ARMED_KEY = "sync:push:armed"

# Within an HTTP request, scopes are gathered here and applied once the
# response has gone out (LiveSyncMiddleware). Outside a request (a worker, a
# scheduled sweep) they are applied as soon as the commit lands.
_collector: ContextVar[Optional[set]] = ContextVar("live_sync_collector", default=None)

# Background tasks are kept referenced until done, or asyncio may drop them.
_tasks: set = set()

# Off under APP_ENV=testing (set in app.main) so the suite's thousands of
# writes schedule no pushes; the live-sync tests switch it back on.
enabled = True


# ---------------------------------------------------------------------------
# What counts as a change
# ---------------------------------------------------------------------------

# Product columns no customer sees change: search index upkeep, counters,
# costs, SEO text.
_PRODUCT_QUIET = {
    "search_keywords", "search_tokens", "search_name_tokens", "search_phrases",
    "search_index_version", "search_indexed_at", "view_count", "cost_price",
    "meta_title", "meta_description", "updated_at", "created_at",
}
_STOCK = {"stock_quantity", "reserved_quantity", "low_stock_threshold"}
_COUPON_QUIET = {"used_count", "budget_spent", "updated_at", "created_at"}


def _changed(obj) -> set[str]:
    return {a.key for a in inspect(obj).attrs if a.history.has_changes()}


def _before_after(obj, key: str):
    """(value before this flush, value now) for one attribute."""
    hist = inspect(obj).attrs[key].history
    now = getattr(obj, key, None)
    before = hist.deleted[0] if hist.deleted else now
    return before, now


def _in_stock_flipped(obj, has_reserved: bool) -> bool:
    stock_before, stock_now = _before_after(obj, "stock_quantity")
    if has_reserved:
        res_before, res_now = _before_after(obj, "reserved_quantity")
    else:
        res_before = res_now = 0
    before = (stock_before or 0) - (res_before or 0) > 0
    now = (stock_now or 0) - (res_now or 0) > 0
    return before != now


def scopes_for(obj, state: str) -> set[str]:
    """The groups one new / changed / deleted row belongs to. Empty = not news."""
    from app.models.banner import Banner
    from app.models.category import BranchCategory, Category
    from app.models.coupon import Coupon
    from app.models.loyalty import LoyaltyAccount, OrderReward
    from app.models.notification import Notification
    from app.models.order import Order
    from app.models.product import (
        BranchInventory,
        Product,
        ProductImage,
        ProductVariant,
        Review,
        VariantOption,
    )
    from app.models.wallet import Wallet, WalletTransaction

    dirty = state == "dirty"

    if isinstance(obj, (Order, Notification, Wallet, WalletTransaction, OrderReward, LoyaltyAccount)):
        return {f"user:{obj.user_id}"} if getattr(obj, "user_id", None) else set()

    if isinstance(obj, BranchInventory):
        if dirty:
            changed = _changed(obj)
            loud = changed - _STOCK - {"updated_at", "created_at"}
            if not loud and not (changed & _STOCK and _in_stock_flipped(obj, True)):
                return set()
        return {f"catalog:{obj.branch_id}"}

    if isinstance(obj, BranchCategory):
        return {f"catalog:{obj.branch_id}"}

    if isinstance(obj, Product):
        if dirty:
            changed = _changed(obj)
            loud = changed - _PRODUCT_QUIET - _STOCK
            if not loud and not (changed & _STOCK and _in_stock_flipped(obj, False)):
                return set()
        return {"catalog:all"}

    if isinstance(obj, (ProductImage, ProductVariant, VariantOption, Category, Review)):
        if dirty and not (_changed(obj) - {"updated_at", "created_at"}):
            return set()
        return {"catalog:all"}

    if isinstance(obj, Banner):
        return {"banners"}

    if isinstance(obj, Coupon):
        if dirty and not (_changed(obj) - _COUPON_QUIET):
            return set()
        return {"offers"}

    return set()


# ---------------------------------------------------------------------------
# Session hooks
# ---------------------------------------------------------------------------


def _after_flush(session, flush_context) -> None:
    if not enabled:
        return
    found: set[str] = set()
    for state, objs in (("new", session.new), ("dirty", session.dirty), ("deleted", session.deleted)):
        for obj in objs:
            try:
                found |= scopes_for(obj, state)
            except Exception as exc:  # noqa: BLE001 -- noticing must never break a write
                logger.debug(f"live_sync: could not classify {type(obj).__name__}: {exc}")
    if found:
        session.info.setdefault("live_sync", set()).update(found)


def _after_commit(session) -> None:
    scopes = session.info.pop("live_sync", None)
    if scopes:
        dispatch(scopes)


def _after_soft_rollback(session, previous_transaction) -> None:
    session.info.pop("live_sync", None)


def install() -> None:
    """Hook every ORM session in the process. Idempotent."""
    if not event.contains(Session, "after_flush", _after_flush):
        event.listen(Session, "after_flush", _after_flush)
        event.listen(Session, "after_commit", _after_commit)
        event.listen(Session, "after_soft_rollback", _after_soft_rollback)


def dispatch(scopes: Iterable[str]) -> None:
    scopes = set(scopes)
    if not scopes or not enabled:
        return
    collected = _collector.get()
    if collected is not None:
        collected |= scopes
        return
    _spawn(apply(scopes))


def _spawn(coro) -> None:
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        coro.close()
        return
    task = loop.create_task(coro)
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


# ---------------------------------------------------------------------------
# Versions and the debounced push
# ---------------------------------------------------------------------------


def _redis():
    from app.config.redis import get_redis_client

    return get_redis_client()


async def apply(scopes: Iterable[str]) -> None:
    """Bump each scope's version; arm one debounced push for catalog/banners."""
    redis = _redis()
    if redis is None:
        return
    scopes = set(scopes)
    try:
        pipe = redis.pipeline()
        for scope in scopes:
            pipe.incr(_VERSION_KEY.format(scope))
        await pipe.execute()

        pushable = {s for s in scopes if s.startswith("catalog:") or s == "banners"}
        if pushable:
            await redis.sadd(_PENDING_KEY, *pushable)
            # Only the first change in a quiet period schedules the push; the
            # rest just join the pending set. NX makes that true across workers.
            if await redis.set(_ARMED_KEY, "1", nx=True, ex=int(PUSH_DEBOUNCE_SECONDS) + 60):
                _spawn(_push_after_debounce())
    except Exception as exc:  # noqa: BLE001 -- the app's timer still catches up
        logger.warning(f"live_sync: could not record {sorted(scopes)}: {exc}")


def push_payload(pending: Iterable[str]) -> dict[str, str]:
    """The silent message for a batch of pending scopes."""
    pending = set(pending)
    groups = sorted({"banners" if s == "banners" else "catalog" for s in pending})
    branches = sorted(s.split(":", 1)[1] for s in pending if s.startswith("catalog:"))
    return {
        "type": PUSH_TYPE,
        "groups": ",".join(groups),
        # "all" when any product-level change is in the batch: every branch shows it.
        "branches": "all" if "all" in branches else ",".join(branches),
    }


async def _push_after_debounce() -> None:
    await asyncio.sleep(PUSH_DEBOUNCE_SECONDS)
    redis = _redis()
    if redis is None:
        return
    try:
        pipe = redis.pipeline()
        pipe.smembers(_PENDING_KEY)
        pipe.delete(_PENDING_KEY)
        pipe.delete(_ARMED_KEY)
        members, _, _ = await pipe.execute()
        pending = {m.decode() if isinstance(m, bytes) else m for m in members or ()}
        if not pending:
            return
        from app.services.fcm_service import FCMService

        await FCMService.send_data_to_topic(PUSH_TOPIC, push_payload(pending))
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"live_sync: push failed: {exc}")


async def versions(branch_id: Optional[str], user_id: Optional[str]) -> dict[str, Optional[str]]:
    """What GET /app/sync-state returns. None everywhere when Redis is down,
    which tells the app to re-read rather than trust a version it cannot get."""
    redis = _redis()
    keys = ["catalog:all", f"catalog:{branch_id}" if branch_id else None, "banners", "offers",
            f"user:{user_id}" if user_id else None]
    if redis is None:
        return {"catalog": None, "banners": None, "offers": None, "user": None}
    try:
        values = await redis.mget([_VERSION_KEY.format(k) for k in keys if k])
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"live_sync: versions unavailable: {exc}")
        return {"catalog": None, "banners": None, "offers": None, "user": None}

    it = iter(values)
    got = {k: (next(it) if k else None) for k in keys}

    def v(key):
        raw = got.get(key)
        if raw is None:
            return "0"
        return raw.decode() if isinstance(raw, bytes) else str(raw)

    return {
        "catalog": f"{v('catalog:all')}.{v(keys[1]) if keys[1] else '-'}",
        "banners": v("banners"),
        "offers": v("offers"),
        "user": v(keys[4]) if keys[4] else None,
    }


class LiveSyncMiddleware:
    """Gather a request's changes and apply them once, after the response."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        collected: set = set()
        token = _collector.set(collected)
        try:
            await self.app(scope, receive, send)
        finally:
            _collector.reset(token)
            if collected:
                await apply(collected)


async def drain() -> None:
    """Wait for every scheduled apply/push (tests)."""
    while _tasks:
        await asyncio.gather(*list(_tasks), return_exceptions=True)
