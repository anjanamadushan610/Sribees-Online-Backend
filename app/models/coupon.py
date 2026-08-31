"""
Coupon SQLAlchemy Model

Promotions & coupon codes managed by Marketing Managers / Super Admins.
Discount values are stored server-side so the checkout flow can (in future)
validate applied coupons against this table instead of trusting client input.
"""
import enum
import uuid

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Integer,
    Numeric,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import UUID

from app.config.database import Base


class CouponDiscountType(str, enum.Enum):
    """How a coupon's discount_value is interpreted."""
    PERCENTAGE = "percentage"
    FIXED = "fixed"


class Coupon(Base):
    """A promotional discount code."""

    __tablename__ = "coupons"

    coupon_id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    code = Column(String(50), unique=True, nullable=False, index=True)
    description = Column(Text, nullable=True)

    # "percentage" (0-100) or "fixed" (absolute currency amount).
    discount_type = Column(String(20), nullable=False, default=CouponDiscountType.PERCENTAGE.value)
    discount_value = Column(Numeric(10, 2), nullable=False)

    # Minimum cart subtotal required to apply the coupon.
    min_order_value = Column(Numeric(10, 2), nullable=False, default=0)

    # Optional cap for percentage discounts.
    max_discount_amount = Column(Numeric(10, 2), nullable=True)

    # NULL usage_limit = unlimited redemptions (across all customers).
    usage_limit = Column(Integer, nullable=True)
    used_count = Column(Integer, nullable=False, default=0)

    # NULL per_user_limit = unlimited per customer; otherwise the max number of
    # times one customer may redeem this code (tracked in `user_coupons`).
    # New coupons default to 1 (Python-side); pre-existing rows stay NULL so
    # migration 048 doesn't silently cap live promos.
    per_user_limit = Column(Integer, nullable=True, default=1)

    # True → the code is listed in the mobile app's "Available Offers" and a
    # customer must *collect* it (a `user_coupons` row) before it can be
    # applied. False → a private code shared out-of-band, applied by typing it,
    # exactly as before.
    is_public = Column(Boolean, nullable=False, default=False, server_default="false")

    valid_from = Column(DateTime(timezone=True), nullable=False)
    valid_until = Column(DateTime(timezone=True), nullable=False)

    is_active = Column(Boolean, nullable=False, default=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    def __repr__(self) -> str:
        return f"<Coupon {self.code} ({self.discount_type})>"
