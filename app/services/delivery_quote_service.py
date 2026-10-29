"""
Delivery quotes for a shopper — the one path from "this cart, this address" to
a SribeesExpress price.

Three screens ask the same question and must get the same answer: the cart
(GET /cart), the checkout preview (POST /orders/quote) and order creation
(POST /orders). What changes between them is only how fresh the answer must be:

* The **cart** may reuse a quote for a few minutes
  (``settings.cart_delivery_quote_cache_seconds``). It is re-read on every
  quantity tap, and asking the courier each time would spend the account's
  60-requests-a-minute budget on people browsing.
* **Checkout** never reuses one. The quote an order carries is the quote that
  gets confirmed and later booked, it is single-use, and it has to be for the
  cart as it is at the moment of payment.

Who prices the parcel is decided here too. The fulfilling branch comes from the
address's postal city, and the quote is made inside that branch's credential
scope: a branch holding its own key is its own SribeesExpress client, priced
from its own registered postal city. A branch using the shared account key is
priced from the SribeesExpress outlet registered for it, which is why
``outlet_id`` is sent only in that case.
"""
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import List, Optional
from uuid import UUID

from loguru import logger
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import settings
from app.models.branch import Branch
from app.models.user import Address
from app.services import branch_service, courier_credentials, courier_location_service
from app.services.pricing_service import (
    DELIVERY_NO_ADDRESS,
    DELIVERY_NOT_SERVICEABLE,
    DELIVERY_QUOTED,
    DeliveryQuote,
    PricingService,
    cart_weight_kg,
)
from app.services.referral_promotion_service import ReferralPromotionService
from app.services.referral_promotion_service import enabled as promotions_enabled


_CACHE_PREFIX = "courier:cart-quote:v1"


@dataclass
class DeliveryDestination:
    """Where a cart is going, and who ships it."""

    address: Optional[Address]
    branch_id: Optional[UUID]
    postal_city_id: Optional[int]

    @property
    def postal_city(self) -> Optional[str]:
        return self.address.postal_city if self.address is not None else None


async def default_address(db: AsyncSession, user_id: UUID) -> Optional[Address]:
    """The shopper's default address, else the one they saved most recently."""
    return (
        await db.execute(
            select(Address)
            .where(Address.user_id == user_id)
            .order_by(Address.is_default.desc(), Address.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def destination_for(
    db: AsyncSession, user_id: UUID, address_id: Optional[str] = None
) -> DeliveryDestination:
    """
    Resolve the address a cart is priced for.

    An ``address_id`` that is not the caller's own is treated as no address at
    all, never as someone else's — the ownership check is the same one every
    address route applies.
    """
    address: Optional[Address] = None
    if address_id:
        try:
            address = await branch_service.get_address_for_user(db, UUID(str(address_id)), user_id)
        except ValueError:
            address = None
    else:
        address = await default_address(db, user_id)

    if address is None:
        return DeliveryDestination(address=None, branch_id=None, postal_city_id=None)

    mapping = await branch_service.resolve_branch_by_postal_city(db, address.postal_city)
    postal_city_id = await courier_location_service.postal_city_id_for_address(db, address)
    return DeliveryDestination(
        address=address,
        branch_id=mapping.branch_id if mapping is not None else None,
        postal_city_id=postal_city_id,
    )


async def outlet_for(db: AsyncSession, branch_id: Optional[UUID]) -> Optional[int]:
    """
    The SribeesExpress outlet a quote inside the active scope must name.

    Only under the shared account key: that account's own registered address is
    head office, not this branch. A branch with its own key is priced from its
    own registration and names nothing.
    """
    target = courier_credentials.active_target()
    if target is None or target.key_source != "account" or branch_id is None:
        return None
    return (
        await db.execute(select(Branch.courier_outlet_id).where(Branch.branch_id == branch_id))
    ).scalar_one_or_none()


def _cache_key(
    target: courier_credentials.CourierTarget,
    postal_city_id: int,
    outlet_id: Optional[int],
    weight_kg: Decimal,
    payment_method: str,
) -> str:
    # The account is identified by a digest of its host and key, never the key
    # itself: Redis keys show up in MONITOR output and memory dumps.
    account = hashlib.sha256(f"{target.base_url}|{target.api_key}".encode()).hexdigest()[:16]
    return (
        f"{_CACHE_PREFIX}:{account}:{postal_city_id}:{outlet_id or 0}:"
        f"{weight_kg.quantize(Decimal('0.001'))}:{payment_method}"
    )


async def _read_cached(redis: Optional[Redis], key: str) -> Optional[DeliveryQuote]:
    if redis is None:
        return None
    try:
        raw = await redis.get(key)
    except Exception as exc:  # pragma: no cover - cache is best-effort
        logger.warning(f"Cart delivery quote cache read failed: {exc}")
        return None
    if not raw:
        return None
    try:
        data = json.loads(raw)
        return DeliveryQuote(
            status=DELIVERY_QUOTED,
            fee=Decimal(data["fee"]),
            expires_at=datetime.fromisoformat(data["expires_at"]) if data.get("expires_at") else None,
        )
    except (ValueError, KeyError, TypeError):
        return None


async def _write_cached(redis: Optional[Redis], key: str, quote: DeliveryQuote) -> None:
    ttl = settings.cart_delivery_quote_cache_seconds
    if redis is None or ttl <= 0 or quote.status != DELIVERY_QUOTED or quote.fee is None:
        return
    # The quote id is not cached: a cart never books, and a cached id handed to
    # checkout would be single-use across every shopper who read it.
    payload = {
        "fee": str(quote.fee),
        "expires_at": quote.expires_at.isoformat() if quote.expires_at else None,
    }
    try:
        await redis.set(key, json.dumps(payload), ex=ttl)
    except Exception as exc:  # pragma: no cover - cache is best-effort
        logger.warning(f"Cart delivery quote cache write failed: {exc}")


async def quote_for_cart(
    db: AsyncSession,
    redis: Optional[Redis],
    user_id: UUID,
    items: List[dict],
    *,
    address_id: Optional[str] = None,
    payment_method: str = "cod",
) -> tuple[DeliveryQuote, DeliveryDestination]:
    """
    The delivery charge to show under a cart. Never raises.

    ``no_address`` when the shopper has not saved one; ``not_serviceable`` when
    their postal city is not one SribeesExpress delivers to (it did not match the
    synced directory, or no branch serves it); otherwise whatever the courier
    answers, reused from the cache when the same account priced the same
    postal city, weight and payment method a few minutes ago.
    """
    destination = await destination_for(db, user_id, address_id)
    if not items:
        return await PricingService.quote_delivery(items, postal_city_id=None), destination
    if destination.address is None:
        return DeliveryQuote(status=DELIVERY_NO_ADDRESS), destination
    if destination.postal_city_id is None or destination.branch_id is None:
        return DeliveryQuote(status=DELIVERY_NOT_SERVICEABLE), destination

    from app.services.order_service import OrderService

    if promotions_enabled():
        # Quote requests may carry stale/client-supplied prices. Eligibility
        # must use the same catalogue subtotal order creation will charge.
        items = await OrderService._attach_server_prices(db, items, destination.branch_id)
    weighted = await OrderService._attach_weights(db, items)
    async with courier_credentials.optional_for_branch(db, destination.branch_id) as target:
        outlet_id = await outlet_for(db, destination.branch_id)
        key = (
            _cache_key(target, destination.postal_city_id, outlet_id, cart_weight_kg(weighted), payment_method)
            if target is not None
            else None
        )
        if key is not None:
            cached = await _read_cached(redis, key)
            if cached is not None:
                subtotal = sum(Decimal(str(i.get('price', 0))) * Decimal(str(i['quantity'])) for i in weighted)
                return await ReferralPromotionService.quote(db, user_id, subtotal, cached), destination
        quote = await PricingService.quote_delivery(
            weighted,
            postal_city_id=destination.postal_city_id,
            payment_method=payment_method,
            outlet_id=outlet_id,
        )
        if key is not None:
            await _write_cached(redis, key, quote)
    subtotal = sum(Decimal(str(i.get('price', 0))) * Decimal(str(i['quantity'])) for i in weighted)
    return await ReferralPromotionService.quote(db, user_id, subtotal, quote), destination


def delivery_payload(quote: DeliveryQuote, destination: DeliveryDestination) -> dict:
    """The ``delivery`` block the cart and checkout responses carry."""
    return {
        "status": quote.status,
        "available": quote.available,
        "fee": float(quote.fee) if quote.fee is not None else None,
        "message": quote.message,
        "original_fee": float(quote.original_fee) if quote.original_fee is not None else None,
        "promotion": quote.promotion,
        "address_id": str(destination.address.address_id) if destination.address is not None else None,
        "postal_city": destination.postal_city,
    }
