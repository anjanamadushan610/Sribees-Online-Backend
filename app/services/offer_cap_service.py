"""
Offer loss caps: how much below-normal-price goods one customer may take from a
branch offer (owner, 2026-11-11; docs/plans/offer-loss-cap.md).

The loss of a line is (normal price - price charged) x quantity, from the
catalogue, never from the client. One customer's loss from one offer is summed
over every order they placed while it ran (cancelled orders give theirs back;
returns do not) plus the cart in hand. Nothing may take it above the offer's
cap: an add that would is refused, and checkout -- the authoritative gate --
re-checks under the customer's row lock and writes the order's usage in the
same transaction.

The customer never learns the cap. When they reach it the cart says only that
their offer limit is used up (``limit_message``).

The offer's prices are an ordinary Quick Sale: at the start ``apply_due`` sets
each product's branch row to the normal price with the discount that lands on
the offer price (weight options follow the product's discount already), and at
the end it puts the row back exactly as it was.
"""
import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Dict, Iterable, List, Optional, Tuple
from uuid import UUID

from loguru import logger
from sqlalchemy import and_, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.request_language import current_language
from app.models.offer_cap import (
    CANCELLED,
    ENDED,
    LIVE,
    SCHEDULED,
    OfferCap,
    OfferCapItem,
    OfferCapUsage,
)
from app.models.product import BranchInventory, Product

_CENT = Decimal("0.01")
POLL_SECONDS = 30

# The owner's wording (si); en/ta say the same. Never the amount.
_TEXTS = {
    "en": "You have reached your offer limit. This offer item can't be added any more.",
    "si": "ඔබගේ offer සීමාව සම්පූර්ණයි. මෙම offer භාණ්ඩය තවදුරටත් එකතු කළ නොහැක.",
    "ta": "உங்கள் சலுகை வரம்பு நிறைவடைந்தது. இந்த சலுகைப் பொருளை மேலும் சேர்க்க முடியாது.",
}


def limit_message(lang: Optional[str] = None) -> str:
    return _TEXTS.get(lang or current_language(), _TEXTS["en"])


class OfferLimitReached(ValueError):
    """The order or cart would take a customer above an offer's cap."""

    def __init__(self, lang: Optional[str] = None):
        super().__init__(limit_message(lang))


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _money(value) -> Decimal:
    return Decimal(str(value or 0)).quantize(_CENT, rounding=ROUND_HALF_UP)


def _uuid(raw) -> Optional[UUID]:
    try:
        return UUID(str(raw)) if raw else None
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# What a cart takes
# ---------------------------------------------------------------------------

async def live_offers(db: AsyncSession, branch_id: Optional[UUID], now: Optional[datetime] = None) -> Dict[UUID, OfferCap]:
    """
    The branch's offers whose prices are on now: every LIVE offer, past its
    end or not. Its prices stay on until the worker restores them, and the cap
    must hold for exactly as long as the prices do.
    """
    if branch_id is None:
        return {}
    rows = (
        await db.execute(select(OfferCap).where(OfferCap.branch_id == branch_id, OfferCap.status == LIVE))
    ).scalars().all()
    return {o.offer_id: o for o in rows}


async def unit_losses(
    db: AsyncSession, branch_id: Optional[UUID], items: List[dict], offer_ids: Iterable[UUID]
) -> List[Optional[Tuple[UUID, Decimal]]]:
    """
    For each line of *items*: ``(offer_id, loss of one unit)`` when the line is
    an offer product, else None. One unit is what the line's quantity counts --
    a pack, or one weight option.
    """
    offer_ids = list(offer_ids)
    out: List[Optional[Tuple[UUID, Decimal]]] = [None] * len(items)
    if not items or not offer_ids or branch_id is None:
        return out

    from app.services import weight_options_service
    from app.services.product_service import ProductService

    product_ids = {u for u in (_uuid(i.get("product_id")) for i in items) if u}
    offered = dict(
        (
            await db.execute(
                select(OfferCapItem.product_id, OfferCapItem.offer_id).where(
                    OfferCapItem.offer_id.in_(offer_ids), OfferCapItem.product_id.in_(product_ids)
                )
            )
        ).all()
    )
    if not offered:
        return out

    pids = list(offered)
    charged = await ProductService.get_price_map(db, pids, branch_id)
    regular = {
        str(pid): Decimal(str(branch_price if branch_price is not None else price or 0))
        for pid, price, branch_price in (
            await db.execute(
                select(Product.product_id, Product.price, BranchInventory.branch_price)
                .outerjoin(
                    BranchInventory,
                    and_(BranchInventory.product_id == Product.product_id, BranchInventory.branch_id == branch_id),
                )
                .where(Product.product_id.in_(pids))
            )
        ).all()
    }
    variant_ids = [
        u for u in (_uuid(i.get("variant_id")) for i in items if _uuid(i.get("product_id")) in offered) if u
    ]
    v_charged = await weight_options_service.price_map(db, variant_ids, branch_id)
    v_regular = await weight_options_service.regular_price_map(db, variant_ids, branch_id)

    for index, item in enumerate(items):
        pid = _uuid(item.get("product_id"))
        if pid not in offered:
            continue
        vid = str(item.get("variant_id") or "")
        if vid and vid in v_charged and v_charged[vid][0] == str(pid) and vid in v_regular:
            gap = v_regular[vid][1] - v_charged[vid][1]
        elif str(pid) in charged and str(pid) in regular:
            gap = regular[str(pid)] - charged[str(pid)]
        else:
            continue  # not sellable here; checkout refuses it anyway
        out[index] = (offered[pid], max(_money(gap), Decimal("0")))
    return out


async def used(
    db: AsyncSession, user_id: UUID, offer_ids: Iterable[UUID], *, exclude_order_id: Optional[UUID] = None
) -> Dict[UUID, Decimal]:
    """Loss the customer's earlier orders took from each offer (cancelled orders excluded)."""
    offer_ids = list(offer_ids)
    if not offer_ids:
        return {}
    query = (
        select(OfferCapUsage.offer_id, func.coalesce(func.sum(OfferCapUsage.amount), 0))
        .where(
            OfferCapUsage.user_id == user_id,
            OfferCapUsage.offer_id.in_(offer_ids),
            OfferCapUsage.released_at.is_(None),
        )
        .group_by(OfferCapUsage.offer_id)
    )
    if exclude_order_id is not None:
        query = query.where(OfferCapUsage.order_id != exclude_order_id)
    return {oid: _money(total) for oid, total in (await db.execute(query)).all()}


@dataclass
class Verdict:
    over: bool = False  # the cart would take some offer above its cap
    reached: bool = False  # some offer in the cart cannot take one more unit of its lines
    takes: Dict[UUID, Decimal] = field(default_factory=dict)  # this cart's loss per offer


def judge(
    caps: Dict[UUID, Decimal],
    already: Dict[UUID, Decimal],
    lines: List[Tuple[Optional[Tuple[UUID, Decimal]], int]],
) -> Verdict:
    """
    Pure rule. *lines* is ``[(unit_loss or None, quantity)]``. Over = used +
    cart above the cap (never allowed, even by the last item). Reached = over,
    or no line of that offer in the cart could take one more unit.
    """
    takes: Dict[UUID, Decimal] = {}
    smallest: Dict[UUID, Decimal] = {}
    for loss, quantity in lines:
        if loss is None or quantity <= 0:
            continue
        offer_id, per_unit = loss
        takes[offer_id] = takes.get(offer_id, Decimal("0")) + per_unit * quantity
        if per_unit > 0:
            smallest[offer_id] = min(smallest.get(offer_id, per_unit), per_unit)
    verdict = Verdict(takes={k: _money(v) for k, v in takes.items()})
    for offer_id, take in verdict.takes.items():
        cap = caps.get(offer_id)
        if cap is None:
            continue
        total = already.get(offer_id, Decimal("0")) + take
        if total > cap:
            verdict.over = True
            verdict.reached = True
        elif offer_id in smallest and total + smallest[offer_id] > cap:
            verdict.reached = True
    return verdict


async def evaluate(
    db: AsyncSession,
    user_id: Optional[UUID],
    branch_id: Optional[UUID],
    items: List[dict],
    *,
    exclude_order_id: Optional[UUID] = None,
) -> Verdict:
    """The verdict for this customer buying *items* in *branch_id* now."""
    if user_id is None or branch_id is None or not items:
        return Verdict()
    offers = await live_offers(db, branch_id)
    if not offers:
        return Verdict()
    losses = await unit_losses(db, branch_id, items, offers.keys())
    if not any(losses):
        return Verdict()
    already = await used(db, user_id, offers.keys(), exclude_order_id=exclude_order_id)
    caps = {oid: _money(o.cap_amount) for oid, o in offers.items()}
    return judge(caps, already, [(loss, int(item.get("quantity") or 0)) for loss, item in zip(losses, items, strict=True)])


def with_quantity(items: List[dict], product_id: str, variant_id: Optional[str], quantity: int, *, add: bool) -> List[dict]:
    """*items* as they would be after setting (or adding) one line's quantity."""
    out, found = [], False
    for item in items:
        line = dict(item)
        if str(line.get("product_id")) == str(product_id) and (line.get("variant_id") or None) == (variant_id or None):
            line["quantity"] = int(line.get("quantity") or 0) + quantity if add else quantity
            found = True
        out.append(line)
    if not found:
        out.append({"product_id": product_id, "variant_id": variant_id, "quantity": quantity})
    return out


async def guard_cart_change(
    db: AsyncSession, user_id: UUID, branch_id: Optional[UUID], before: List[dict], after: List[dict]
) -> None:
    """
    Refuse a cart change that takes an offer above its cap. A change that only
    lowers what the cart takes is always allowed, so a cart already over (a
    sync from an old app, a cancelled offer coming back) can be fixed.
    """
    verdict = await evaluate(db, user_id, branch_id, after)
    if not verdict.over:
        return
    previous = await evaluate(db, user_id, branch_id, before)
    if all(verdict.takes.get(k, Decimal("0")) <= previous.takes.get(k, Decimal("0")) for k in verdict.takes):
        return
    raise OfferLimitReached()


def cart_block(verdict: Verdict) -> dict:
    """What every cart response says about the limit -- never the amount."""
    return {"reached": verdict.reached, "message": limit_message() if verdict.reached else None}


# ---------------------------------------------------------------------------
# Orders
# ---------------------------------------------------------------------------

def stage_usage(db: AsyncSession, user_id: UUID, order_id: UUID, verdict: Verdict) -> None:
    """Record what the order takes from each offer, in the caller's transaction."""
    for offer_id, amount in verdict.takes.items():
        if amount > 0:
            db.add(OfferCapUsage(offer_id=offer_id, user_id=user_id, order_id=order_id, amount=amount))


async def release_for_order(db: AsyncSession, order_id: UUID) -> None:
    """A cancelled order gives its loss back. Staged in the caller's transaction."""
    await db.execute(
        update(OfferCapUsage)
        .where(OfferCapUsage.order_id == order_id, OfferCapUsage.released_at.is_(None))
        .values(released_at=_now())
    )


async def reclaim_for_order(db: AsyncSession, order_id: UUID) -> None:
    """An override un-cancelling an order takes its loss again. Staged in the caller's transaction."""
    await db.execute(
        update(OfferCapUsage)
        .where(OfferCapUsage.order_id == order_id, OfferCapUsage.released_at.is_not(None))
        .values(released_at=None)
    )


# ---------------------------------------------------------------------------
# Prices on and off
# ---------------------------------------------------------------------------

def discount_for(regular: Decimal, offer_price: Decimal) -> float:
    """The discount % (6 dp) on *regular* that charges exactly *offer_price*."""
    pct = ((regular - offer_price) / regular * 100).quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)
    charged = (regular * Decimal(str(1 - float(pct) / 100))).quantize(_CENT, rounding=ROUND_HALF_UP)
    if charged != offer_price:
        raise ValueError(f"{offer_price} cannot be reached exactly from {regular}")
    return float(pct)


class CannotApply(ValueError):
    """An offer whose prices cannot go on as set (the branch price moved, or a clash)."""


# Every price change an offer makes runs under this one transaction-scoped lock:
# four server processes each run the sweep, the admin can end an offer, and two
# offers may share a product. Serialising them means a start never reads another
# offer's discount as "the price before".
_SWEEP_LOCK = 0x0FFE_C0DE


async def _lock(db: AsyncSession) -> None:
    await db.execute(select(func.pg_advisory_xact_lock(_SWEEP_LOCK)))


async def _apply(db: AsyncSession, offer: OfferCap) -> None:
    items = (await db.execute(select(OfferCapItem).where(OfferCapItem.offer_id == offer.offer_id))).scalars().all()
    clash = await db.scalar(
        select(OfferCapItem.product_id)
        .join(OfferCap, OfferCap.offer_id == OfferCapItem.offer_id)
        .where(
            OfferCap.branch_id == offer.branch_id,
            OfferCap.status == LIVE,
            OfferCap.offer_id != offer.offer_id,
            OfferCapItem.product_id.in_([i.product_id for i in items]),
        )
        .limit(1)
    )
    if clash is not None:
        raise CannotApply(f"product {clash} is still in another live offer")
    for item in items:
        row = await db.scalar(
            select(BranchInventory)
            .where(BranchInventory.branch_id == offer.branch_id, BranchInventory.product_id == item.product_id)
            .with_for_update()
        )
        product = await db.get(Product, item.product_id)
        if row is None or product is None:
            raise CannotApply(f"product {item.product_id} has no row in the branch")
        regular = Decimal(str(row.branch_price if row.branch_price is not None else product.price))
        offer_price = Decimal(str(item.offer_price))
        if offer_price >= regular:
            raise CannotApply(f"product {item.product_id}: offer {offer_price} is not below {regular}")
        item.prev_branch_price = row.branch_price
        item.prev_discount_percentage = row.discount_percentage
        item.prev_is_on_sale = row.is_on_sale
        row.branch_price = regular
        row.discount_percentage = discount_for(regular, offer_price)  # ValueError -> CannotApply below
        row.is_on_sale = True
    offer.status, offer.applied_at = LIVE, _now()


def _still_offer_price(row: BranchInventory, item: OfferCapItem) -> bool:
    """Whether the branch row still charges the offer price (nobody re-priced it during the offer)."""
    if row.branch_price is None or not row.discount_percentage:
        return False
    price = Decimal(str(row.branch_price))
    charged = (price * Decimal(str(1 - row.discount_percentage / 100))).quantize(_CENT, rounding=ROUND_HALF_UP)
    return charged == Decimal(str(item.offer_price))


async def _restore(db: AsyncSession, offer: OfferCap) -> None:
    items = (await db.execute(select(OfferCapItem).where(OfferCapItem.offer_id == offer.offer_id))).scalars().all()
    for item in items:
        row = await db.scalar(
            select(BranchInventory)
            .where(BranchInventory.branch_id == offer.branch_id, BranchInventory.product_id == item.product_id)
            .with_for_update()
        )
        if row is None:
            continue
        if not _still_offer_price(row, item):
            # A manager re-priced it during the offer: their price stands.
            logger.warning(
                f"Offer {offer.name}: product {item.product_id} was re-priced during the offer; left as set"
            )
            continue
        row.branch_price = item.prev_branch_price
        row.discount_percentage = item.prev_discount_percentage
        row.is_on_sale = bool(item.prev_is_on_sale)
    offer.status, offer.restored_at = ENDED, _now()


async def end_now(db: AsyncSession, offer: OfferCap) -> None:
    """Admin's stop: a live offer's prices go back now; one not started is cancelled. Caller commits."""
    await _lock(db)
    await db.refresh(offer)
    if offer.status == LIVE:
        await _restore(db, offer)
    elif offer.status == SCHEDULED:
        offer.status = CANCELLED


async def apply_due(db: AsyncSession, now: Optional[datetime] = None) -> int:
    """
    Start and end the offers that are due, ends first, one offer per
    transaction. An offer that cannot start is cancelled and logged, so it can
    never hold up another offer's end. Returns how many moved.
    """
    now = now or _now()
    moved = 0
    while True:
        await _lock(db)
        offer = await db.scalar(
            select(OfferCap)
            .where((OfferCap.status == LIVE) & (OfferCap.ends_at <= now))
            .order_by(OfferCap.ends_at)
            .limit(1)
        ) or await db.scalar(
            select(OfferCap)
            .where((OfferCap.status == SCHEDULED) & (OfferCap.starts_at <= now))
            .order_by(OfferCap.starts_at)
            .limit(1)
        )
        if offer is None:
            await db.rollback()
            return moved
        offer_id, name = offer.offer_id, offer.name
        try:
            if offer.status == LIVE:
                await _restore(db, offer)
            elif offer.ends_at <= now:
                offer.status = ENDED  # its whole window passed while the server was down
            else:
                await _apply(db, offer)
            await db.commit()
            logger.info(f"Offer {name} ({offer_id}) is now {offer.status}")
        except ValueError as exc:  # CannotApply, or a price that cannot be hit exactly
            await db.rollback()
            await _lock(db)
            await db.execute(update(OfferCap).where(OfferCap.offer_id == offer_id).values(status=CANCELLED))
            await db.commit()
            logger.error(f"Offer {name} ({offer_id}) could not start and was cancelled: {exc}")
        moved += 1


async def run_offer_cap_worker():
    from app.config.database import async_session_maker

    while True:
        try:
            async with async_session_maker() as db:
                await apply_due(db)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Offer cap sweep failed; check migration 111. Offers start/end when it recovers.")
        await asyncio.sleep(POLL_SECONDS)
