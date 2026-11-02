"""
Free delivery: a branch's offer to everyone it delivers to, and grants to
customers a Super Admin chooses.

A branch's period runs from the moment it was switched on, for everyone alike,
whether they are new or have ordered before. Switching it off ends it at once.

The courier's quote is never changed and the courier is still paid: only the
customer's charge is waived, and the order records the amount the business
carries.
"""

import calendar
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional
from uuid import UUID

from sqlalchemy import or_, select

from app.models.free_delivery import FreeDeliveryBranchOffer, FreeDeliveryGrant
from app.services.pricing_service import DeliveryQuote

PROMOTION = "free_delivery"
FROM_BRANCH, FROM_GRANT = "branch", "customer"


def add_months(moment: datetime, months: int) -> datetime:
    """The same day ``months`` later, or that month's last day if it is shorter."""
    index = moment.month - 1 + months
    year, month = moment.year + index // 12, index % 12 + 1
    return moment.replace(year=year, month=month, day=min(moment.day, calendar.monthrange(year, month)[1]))


def offer_ends_at(offer: FreeDeliveryBranchOffer) -> Optional[datetime]:
    return add_months(offer.enabled_at, offer.months) if offer.enabled_at else None


async def live_offer(db, branch_id: Optional[UUID]) -> Optional[FreeDeliveryBranchOffer]:
    """The branch's offer if it is switched on and its period has not run out."""
    if branch_id is None:
        return None
    offer = await db.get(FreeDeliveryBranchOffer, branch_id)
    if offer is None or not offer.enabled or offer.enabled_at is None:
        return None
    return offer if offer_ends_at(offer) > datetime.now(timezone.utc) else None


async def _live_grant(db, user_id: UUID, branch_id: Optional[UUID], subtotal: Decimal) -> Optional[FreeDeliveryGrant]:
    now = datetime.now(timezone.utc)
    return await db.scalar(
        select(FreeDeliveryGrant)
        .where(
            FreeDeliveryGrant.user_id == user_id,
            FreeDeliveryGrant.revoked_at.is_(None),
            FreeDeliveryGrant.starts_at <= now,
            FreeDeliveryGrant.ends_at > now,
            FreeDeliveryGrant.min_order <= subtotal,
            or_(FreeDeliveryGrant.branch_id.is_(None), FreeDeliveryGrant.branch_id == branch_id),
        )
        .limit(1)
    )


async def covering_source(db, user_id: UUID, branch_id: Optional[UUID], subtotal: Decimal) -> Optional[str]:
    """Why delivery on this order is free (FROM_BRANCH or FROM_GRANT), or None."""
    offer = await live_offer(db, branch_id)
    if offer is not None and offer.min_order <= subtotal:
        return FROM_BRANCH
    return FROM_GRANT if await _live_grant(db, user_id, branch_id, subtotal) else None


async def apply_to_quote(db, user_id, branch_id, subtotal, quote: DeliveryQuote) -> Optional[DeliveryQuote]:
    """The quote with the charge waived, or None when nothing covers it."""
    if user_id is None or not quote.available or not quote.fee or quote.fee <= 0:
        return None
    if await covering_source(db, user_id, branch_id, subtotal) is None:
        return None
    return replace(quote, fee=Decimal("0.00"), original_fee=quote.fee, promotion=PROMOTION)


async def welcome_offer(db, user_id: Optional[UUID], branch_id: Optional[UUID]) -> Optional[dict]:
    """What the popup after the tour says; None when the branch has no live offer."""
    offer = await live_offer(db, branch_id)
    if offer is None:
        return None
    return {
        "months": offer.months,
        "min_order": float(offer.min_order),
        "ends_at": offer_ends_at(offer).isoformat(),
        "needs_account": user_id is None,
    }
