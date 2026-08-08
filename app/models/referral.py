"""
Referral SQLAlchemy Models

A referrer earns a percentage of what the people they invited spend.

Design notes that matter:

* **The binding is immutable.** ``users.referred_by_user_id`` is written once,
  at the moment the code is redeemed, and never again. If it could be changed
  later, a user could re-point themselves at whoever is paying the most that
  week and the whole ledger becomes unauditable.

* **The rate is snapshotted per commission.** Changing the platform rate must
  not silently rewrite what people already earned, so ``rate_percentage`` is
  stored on the row rather than looked up at read time.

* **The level is stored, not inferred.** App invites are level 1 only, but the
  professional referral team needs a second level. Storing it now means adding
  that later is a config change, not a migration over historical rows.

* **``order_subtotal`` never leaves the server.** It is kept for audit and for
  support to answer "why is this Rs 45", but the referrer is only ever shown
  the commission — a referrer does not get to see their friend's basket value.
"""
import uuid

from sqlalchemy import (
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Numeric,
    SmallInteger,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from app.config.database import Base


class ReferralCommissionStatus:
    """Lifecycle of a single commission row."""

    # Order placed; nothing is owed yet because it can still be cancelled.
    PENDING = "pending"
    # Order delivered. The money is real and the referrer may claim it.
    CLAIMABLE = "claimable"
    # Moved into the referrer's wallet.
    PAID = "paid"
    # Order cancelled or refunded. Never payable; clawed back if already paid.
    REVERSED = "reversed"

    ALL = (PENDING, CLAIMABLE, PAID, REVERSED)


class ReferralCommission(Base):
    """
    One row per (order, beneficiary, level).

    The unique constraint on that triple is the idempotency guard: accrual can
    run twice — a retried webhook, a replayed status transition — without ever
    paying twice.
    """

    __tablename__ = "referral_commissions"

    commission_id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    order_id = Column(
        UUID(as_uuid=True),
        ForeignKey("orders.order_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # Denormalised so support can search by the number the customer quotes,
    # and so a deleted order still reads sensibly in the ledger.
    order_number = Column(String(50), nullable=True)

    beneficiary_user_id = Column(
        UUID(as_uuid=True),
        ForeignKey("users.user_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="Who earns this commission (the referrer)",
    )
    source_user_id = Column(
        UUID(as_uuid=True),
        ForeignKey("users.user_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="Who placed the order (the referred friend)",
    )

    level = Column(
        SmallInteger,
        nullable=False,
        default=1,
        server_default="1",
        comment="1 = direct invite. 2 = professional referral team's second tier.",
    )

    rate_percentage = Column(
        Float,
        nullable=False,
        comment="Commission rate applied, snapshotted at order time",
    )
    order_subtotal = Column(
        Numeric(10, 2),
        nullable=False,
        comment="Basis the commission was computed on. Internal/audit only — "
                "never exposed to the beneficiary.",
    )
    amount = Column(
        Numeric(10, 2),
        nullable=False,
        comment="Commission earned (order_subtotal * rate_percentage / 100)",
    )

    status = Column(
        String(20),
        nullable=False,
        default=ReferralCommissionStatus.PENDING,
        server_default=ReferralCommissionStatus.PENDING,
        index=True,
    )

    claimed_at = Column(DateTime(timezone=True), nullable=True)
    wallet_transaction_id = Column(
        UUID(as_uuid=True),
        ForeignKey("wallet_transactions.transaction_id", ondelete="SET NULL"),
        nullable=True,
        comment="The credit that paid this out; proves it was settled once",
    )

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    beneficiary = relationship("User", foreign_keys=[beneficiary_user_id])
    source_user = relationship("User", foreign_keys=[source_user_id])

    __table_args__ = (
        # Idempotency: one commission per order, per beneficiary, per level.
        UniqueConstraint(
            "order_id",
            "beneficiary_user_id",
            "level",
            name="uq_referral_commission_order_beneficiary_level",
        ),
        # The two read patterns: "my earnings summary" and "what did this
        # friend earn me".
        Index("idx_referral_comm_beneficiary_status", "beneficiary_user_id", "status"),
        Index("idx_referral_comm_beneficiary_source", "beneficiary_user_id", "source_user_id"),
    )

    def __repr__(self):
        return (
            f"<ReferralCommission {self.amount} L{self.level} "
            f"{self.status} order={self.order_number}>"
        )
