"""
User Coupon Model — a customer's "collected" coupon.

One row per (user, coupon): the customer tapped Collect in the mobile app's
Available Offers, or (for a private code) redeemed it by typing it. `times_redeemed`
tracks usage against the coupon's `per_user_limit`.
"""
import uuid

from sqlalchemy import Column, DateTime, ForeignKey, Integer, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from app.config.database import Base


class UserCoupon(Base):
    """A coupon a customer has collected (and possibly redeemed)."""

    __tablename__ = "user_coupons"

    user_coupon_id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(
        UUID(as_uuid=True),
        ForeignKey("users.user_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    coupon_id = Column(
        UUID(as_uuid=True),
        ForeignKey("coupons.coupon_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    collected_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    # Redemptions of this coupon by this customer, checked against
    # coupons.per_user_limit. A private code redeemed without ever being
    # "collected" still gets a row here (collected_at == first redemption).
    times_redeemed = Column(Integer, nullable=False, default=0, server_default="0")
    last_redeemed_at = Column(DateTime(timezone=True), nullable=True)
    last_redeemed_order_id = Column(
        UUID(as_uuid=True),
        ForeignKey("orders.order_id", ondelete="SET NULL"),
        nullable=True,
    )

    coupon = relationship("Coupon")
    user = relationship("User")

    __table_args__ = (
        UniqueConstraint("user_id", "coupon_id", name="uq_user_coupons_user_coupon"),
    )

    def __repr__(self) -> str:
        return f"<UserCoupon user={self.user_id} coupon={self.coupon_id} used={self.times_redeemed}>"
