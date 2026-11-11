"""
Offer loss caps (migration 111, owner 2026-11-11).

A branch offer puts chosen products on Quick Sale below their normal price for a
period. Each customer may take at most ``cap_amount`` of "loss" (normal price -
offer price, times quantity) from one offer, over all their orders. The amount is
never shown to the customer. See docs/plans/offer-loss-cap.md.
"""

import uuid

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Numeric,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID

from app.config.database import Base

SCHEDULED = "scheduled"
LIVE = "live"
ENDED = "ended"
CANCELLED = "cancelled"


class OfferCap(Base):
    """One offer in one branch: its cap, its window, and where it is in its life."""

    __tablename__ = "offer_caps"
    offer_id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    branch_id = Column(UUID(as_uuid=True), ForeignKey("branches.branch_id", ondelete="CASCADE"), nullable=False)
    name = Column(String(120), nullable=False)
    # Per customer, over the whole offer. Hidden from customers.
    cap_amount = Column(Numeric(10, 2), nullable=False)
    starts_at = Column(DateTime(timezone=True), nullable=False)
    ends_at = Column(DateTime(timezone=True), nullable=False)
    # scheduled -> live (prices applied) -> ended (prices restored); or cancelled
    # before it started.
    status = Column(String(16), nullable=False, default=SCHEDULED, server_default=SCHEDULED)
    applied_at = Column(DateTime(timezone=True), nullable=True)
    restored_at = Column(DateTime(timezone=True), nullable=True)
    created_by = Column(UUID(as_uuid=True), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    __table_args__ = (
        Index("ix_offer_caps_branch_status", "branch_id", "status"),
        CheckConstraint("cap_amount > 0", name="ck_offer_caps_cap_positive"),
        CheckConstraint("ends_at > starts_at", name="ck_offer_caps_window"),
        CheckConstraint(
            "status IN ('scheduled', 'live', 'ended', 'cancelled')", name="ck_offer_caps_status"
        ),
    )


class OfferCapItem(Base):
    """A product in an offer: its offer price, and the branch row as it was before."""

    __tablename__ = "offer_cap_items"
    offer_id = Column(UUID(as_uuid=True), ForeignKey("offer_caps.offer_id", ondelete="CASCADE"), primary_key=True)
    product_id = Column(UUID(as_uuid=True), ForeignKey("products.product_id", ondelete="CASCADE"), primary_key=True)
    # Per unit (per kg for a product sold by weight, as its price is).
    offer_price = Column(Numeric(10, 2), nullable=False)
    # branch_inventory as it was when the offer started; put back at the end.
    prev_branch_price = Column(Numeric(10, 2), nullable=True)
    prev_discount_percentage = Column(Float, nullable=True)
    prev_is_on_sale = Column(Boolean, nullable=True)
    __table_args__ = (CheckConstraint("offer_price > 0", name="ck_offer_cap_items_price_positive"),)


class OfferCapUsage(Base):
    """How much loss one order took from one offer. Released when the order is cancelled."""

    __tablename__ = "offer_cap_usage"
    usage_id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    offer_id = Column(UUID(as_uuid=True), ForeignKey("offer_caps.offer_id", ondelete="CASCADE"), nullable=False)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.user_id", ondelete="CASCADE"), nullable=False)
    order_id = Column(UUID(as_uuid=True), ForeignKey("orders.order_id", ondelete="CASCADE"), nullable=False)
    amount = Column(Numeric(10, 2), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    released_at = Column(DateTime(timezone=True), nullable=True)
    __table_args__ = (
        UniqueConstraint("offer_id", "order_id", name="uq_offer_cap_usage_offer_order"),
        CheckConstraint("amount >= 0", name="ck_offer_cap_usage_amount"),
        Index("ix_offer_cap_usage_user_offer", "user_id", "offer_id"),
        Index("ix_offer_cap_usage_order", "order_id"),
    )
