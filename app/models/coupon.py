"""
Coupon SQLAlchemy Model

Promotions & coupon codes managed by Marketing Managers / Super Admins.
Discount values are stored server-side so the checkout flow can (in future)
validate applied coupons against this table instead of trusting client input.
"""
import enum
import uuid
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

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

    # Owning branch. NULL = network-wide, which only a Super Admin may create:
    # a branch's Marketing Manager is pinned to their own branch so a Colombo
    # promotion can never be redeemed against a Galle order. Redemption checks
    # match this against the order's fulfilling branch.
    branch_id = Column(
        UUID(as_uuid=True),
        ForeignKey("branches.branch_id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )

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

    # ---------------------------------------------------------------
    # Financial protection
    # ---------------------------------------------------------------
    # usage_limit caps how MANY times a code is used; this caps how much it
    # COSTS. A 10% coupon with a 2000-redemption limit on an average Rs 4,000
    # basket is an Rs 800,000 liability nobody signed off on — money is the
    # thing a branch has a finite amount of, so money is what we cap.
    # NULL = uncapped.
    budget_cap = Column(Numeric(12, 2), nullable=True)

    # Discount given so far. Denormalised for the same reason used_count is:
    # checkout tests the cap under a row lock, and re-aggregating orders on
    # every redemption would be slow and racy. SUM(orders.discount_amount) for
    # this code (over non-cancelled, non-refunded orders) reconciles against it.
    budget_spent = Column(
        Numeric(12, 2), nullable=False, default=0, server_default="0"
    )

    # Hitting the cap deactivates the coupon rather than merely refusing the
    # next order, so a manager is never left with a code that silently fails
    # for customers while still reading "Active" on the dashboard.
    auto_stop_on_budget = Column(
        Boolean, nullable=False, default=True, server_default="true"
    )

    # Clearance stock is already sold at or below cost; stacking a coupon on it
    # is the double-discounting that turns a promotion into a loss. Excluded
    # lines count toward neither the discount base nor min_order_value.
    exclude_quick_sale = Column(
        Boolean, nullable=False, default=False, server_default="false"
    )

    # ---------------------------------------------------------------
    # Eligibility and targeting
    # ---------------------------------------------------------------
    # Acquisition coupon: only a customer with no prior completed order from
    # the fulfilling branch may redeem it.
    first_order_only = Column(
        Boolean, nullable=False, default=False, server_default="false"
    )

    # Max redemptions by any one customer. NULL = unlimited.
    per_user_limit = Column(Integer, nullable=True)

    # Listed in the mobile "Available Offers" feed. False = a private code that
    # only works for someone who was actually given it.
    is_public = Column(Boolean, nullable=False, default=True, server_default="true")

    # 1-to-1 retention coupon issued to one named customer via the Assign Promo
    # flow on their profile. Nobody else can redeem it. A targeted coupon is
    # always private — the DB enforces that pairing (ck_coupons_targeted_is_private),
    # because a "personal" code discoverable in the public feed is not personal.
    target_user_id = Column(
        UUID(as_uuid=True),
        ForeignKey("users.user_id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )

    is_active = Column(Boolean, nullable=False, default=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    # Catalog eligibility. NO ROWS = the entire catalog is eligible, which is
    # what makes this additive: every coupon that predates the feature keeps
    # applying to everything with no backfill.
    branch = relationship("Branch", lazy="selectin")
    eligible_categories = relationship(
        "CouponCategory", cascade="all, delete-orphan", lazy="selectin"
    )
    eligible_products = relationship(
        "CouponProduct", cascade="all, delete-orphan", lazy="selectin"
    )

    @property
    def is_budget_depleted(self) -> bool:
        """True once the campaign has given away everything it was allotted."""
        if self.budget_cap is None:
            return False
        return Decimal(str(self.budget_spent or 0)) >= Decimal(str(self.budget_cap))

    def __repr__(self) -> str:
        return f"<Coupon {self.code} ({self.discount_type})>"


class CouponCategory(Base):
    """A category a coupon is restricted to (see Coupon.eligible_categories)."""

    __tablename__ = "coupon_categories"

    coupon_id = Column(
        UUID(as_uuid=True),
        ForeignKey("coupons.coupon_id", ondelete="CASCADE"),
        primary_key=True,
    )
    category_id = Column(
        UUID(as_uuid=True),
        ForeignKey("categories.category_id", ondelete="CASCADE"),
        primary_key=True,
    )


class CouponProduct(Base):
    """A product a coupon is restricted to (see Coupon.eligible_products)."""

    __tablename__ = "coupon_products"

    coupon_id = Column(
        UUID(as_uuid=True),
        ForeignKey("coupons.coupon_id", ondelete="CASCADE"),
        primary_key=True,
    )
    product_id = Column(
        UUID(as_uuid=True),
        ForeignKey("products.product_id", ondelete="CASCADE"),
        primary_key=True,
    )
