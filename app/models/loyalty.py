"""
Loyalty SQLAlchemy Models — order rewards, points and levels.

Design notes that matter:

* **An order's reward waits for the return window.** ``OrderReward`` holds the
  cashback and points an order will earn until the goods are delivered and
  the twelve-hour return window has closed. Only then does money reach the
  wallet, so a cancelled or returned order never has to be clawed back out of
  a balance the customer may already have spent.

* **Points never go down.** ``LoyaltyAccount.lifetime_points`` only grows. A
  level is read from it against ``LoyaltyTier`` at request time, so a Super
  Admin editing thresholds takes effect immediately and never rewrites the
  points anyone earned.

* **One of each, enforced by the database.** One reward per order
  (``uq_order_rewards_order``), one level coupon per customer per level
  (``loyalty_tier_rewards`` primary key). Accrual and issuing can run twice
  without paying twice.

Every constraint here is also in migration 066. ``create_all()`` builds these
tables on first boot, before the SQL files run, so the models must carry them.
"""
import uuid

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID

from app.config.database import Base


class OrderRewardStatus:
    """Lifecycle of an order's reward."""

    # Held: the order can still be cancelled or returned.
    PENDING = "pending"
    # Return window closed: cashback credited, points added. Final.
    SETTLED = "settled"
    # Order cancelled before it settled. Nothing was paid.
    REVERSED = "reversed"

    ALL = (PENDING, SETTLED, REVERSED)


class LoyaltyTier(Base):
    """A level a customer reaches at ``min_points`` lifetime points."""

    __tablename__ = "loyalty_tiers"

    tier_id = Column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=text("gen_random_uuid()"),
    )
    name = Column(String(50), nullable=False)
    name_si = Column(String(50), nullable=True)
    name_ta = Column(String(50), nullable=True)
    min_points = Column(Integer, nullable=False)
    color = Column(String(7), nullable=True)
    is_active = Column(Boolean, nullable=False, default=True, server_default="true")

    # The coupon issued once to each customer who reaches this level.
    # reward_discount_type NULL = this level gives no coupon.
    reward_discount_type = Column(String(20), nullable=True)
    reward_discount_value = Column(Numeric(10, 2), nullable=True)
    reward_min_order_value = Column(
        Numeric(10, 2), nullable=False, default=0, server_default="0"
    )
    reward_max_discount = Column(Numeric(10, 2), nullable=True)
    reward_valid_days = Column(Integer, nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        UniqueConstraint("min_points", name="uq_loyalty_tiers_min_points"),
        CheckConstraint("min_points >= 0", name="ck_loyalty_tiers_min_points"),
        CheckConstraint(
            "reward_discount_type IN ('percentage', 'fixed')",
            name="ck_loyalty_tiers_reward_type",
        ),
        CheckConstraint(
            "reward_discount_value > 0", name="ck_loyalty_tiers_reward_value"
        ),
        CheckConstraint(
            "reward_min_order_value >= 0", name="ck_loyalty_tiers_reward_min_order"
        ),
        CheckConstraint(
            "reward_max_discount > 0", name="ck_loyalty_tiers_reward_max_discount"
        ),
        CheckConstraint(
            "reward_valid_days > 0", name="ck_loyalty_tiers_reward_valid_days"
        ),
        CheckConstraint(
            "reward_discount_type IS NULL OR "
            "(reward_discount_value IS NOT NULL AND reward_valid_days IS NOT NULL)",
            name="ck_loyalty_tiers_reward_complete",
        ),
    )

    @property
    def has_reward(self) -> bool:
        return self.reward_discount_type is not None

    def __repr__(self):
        return f"<LoyaltyTier {self.name} @{self.min_points}>"


class LoyaltyAccount(Base):
    """A customer's points. Only ever grows."""

    __tablename__ = "loyalty_accounts"

    user_id = Column(
        UUID(as_uuid=True),
        ForeignKey("users.user_id", ondelete="CASCADE"),
        primary_key=True,
    )
    lifetime_points = Column(Integer, nullable=False, default=0, server_default="0")
    # Rupees earned toward the next point; always below one point's worth.
    spend_carry = Column(Numeric(12, 2), nullable=False, default=0, server_default="0")
    lifetime_spend = Column(Numeric(14, 2), nullable=False, default=0, server_default="0")

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        CheckConstraint("lifetime_points >= 0", name="ck_loyalty_accounts_points"),
        CheckConstraint("spend_carry >= 0", name="ck_loyalty_accounts_carry"),
        CheckConstraint("lifetime_spend >= 0", name="ck_loyalty_accounts_spend"),
    )

    def __repr__(self):
        return f"<LoyaltyAccount {self.user_id} {self.lifetime_points}pts>"


class OrderReward(Base):
    """What one order earns: its cashback and its points, held until it vests."""

    __tablename__ = "order_rewards"

    reward_id = Column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=text("gen_random_uuid()"),
    )
    order_id = Column(
        UUID(as_uuid=True),
        ForeignKey("orders.order_id", ondelete="CASCADE"),
        nullable=False,
    )
    order_number = Column(String(50), nullable=True)
    user_id = Column(
        UUID(as_uuid=True),
        ForeignKey("users.user_id", ondelete="CASCADE"),
        nullable=False,
    )

    # What the order earns now; lowered as goods come back.
    cashback_amount = Column(Numeric(10, 2), nullable=False, default=0, server_default="0")
    status = Column(
        String(20),
        nullable=False,
        default=OrderRewardStatus.PENDING,
        server_default=OrderRewardStatus.PENDING,
    )
    # Delivery + the return window + a minute. NULL until delivered. Stored,
    # not derived, so changing the platform's window later cannot move a
    # reward that was already promised -- the reasoning of migration 058.
    vests_at = Column(DateTime(timezone=True), nullable=True)
    settled_at = Column(DateTime(timezone=True), nullable=True)
    reversed_at = Column(DateTime(timezone=True), nullable=True)

    # The order value the points were counted on, and how many they were.
    qualifying_amount = Column(Numeric(12, 2), nullable=True)
    points_awarded = Column(Integer, nullable=False, default=0, server_default="0")

    # The credit that paid the cashback; proves it was paid once.
    wallet_transaction_id = Column(
        UUID(as_uuid=True),
        ForeignKey("wallet_transactions.transaction_id", ondelete="SET NULL"),
        nullable=True,
    )

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        UniqueConstraint("order_id", name="uq_order_rewards_order"),
        CheckConstraint(
            "status IN ('pending', 'settled', 'reversed')",
            name="ck_order_rewards_status",
        ),
        CheckConstraint("cashback_amount >= 0", name="ck_order_rewards_cashback"),
        CheckConstraint("points_awarded >= 0", name="ck_order_rewards_points"),
        Index("ix_order_rewards_user_status", "user_id", "status"),
        Index(
            "ix_order_rewards_due",
            "vests_at",
            postgresql_where=text("status = 'pending' AND vests_at IS NOT NULL"),
        ),
    )

    def __repr__(self):
        return f"<OrderReward {self.order_number} {self.status} Rs{self.cashback_amount}>"


class LoyaltyTierReward(Base):
    """A level coupon already issued to a customer. The primary key is the once."""

    __tablename__ = "loyalty_tier_rewards"

    user_id = Column(
        UUID(as_uuid=True),
        ForeignKey("users.user_id", ondelete="CASCADE"),
        primary_key=True,
    )
    tier_id = Column(
        UUID(as_uuid=True),
        ForeignKey("loyalty_tiers.tier_id", ondelete="CASCADE"),
        primary_key=True,
    )
    coupon_id = Column(
        UUID(as_uuid=True),
        ForeignKey("coupons.coupon_id", ondelete="SET NULL"),
        nullable=True,
    )
    issued_at = Column(DateTime(timezone=True), server_default=func.now())
