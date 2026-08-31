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

    # NULL usage_limit = unlimited redemptions.
    usage_limit = Column(Integer, nullable=True)
    used_count = Column(Integer, nullable=False, default=0)

    valid_from = Column(DateTime(timezone=True), nullable=False)
    valid_until = Column(DateTime(timezone=True), nullable=False)

    is_active = Column(Boolean, nullable=False, default=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    def __repr__(self) -> str:
        return f"<Coupon {self.code} ({self.discount_type})>"
