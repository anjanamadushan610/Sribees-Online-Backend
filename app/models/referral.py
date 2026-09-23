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

    # Exactly one of these is set — a commission is owed either to a customer
    # who invited a friend, or to a professional partner. Enforced by
    # chk_commission_single_beneficiary in the database.
    beneficiary_user_id = Column(
        UUID(as_uuid=True),
        ForeignKey("users.user_id", ondelete="CASCADE"),
        nullable=True,
        index=True,
        comment="Customer who earns this commission (the referrer)",
    )
    beneficiary_partner_id = Column(
        UUID(as_uuid=True),
        ForeignKey("partners.partner_id", ondelete="CASCADE"),
        nullable=True,
        index=True,
        comment="Professional partner who earns this commission",
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

    # When the money genuinely becomes theirs: delivery plus the same return
    # window the customer gets. Until then the row stays `pending` and nothing
    # can move it.
    #
    # Stored rather than derived from the order, because the window is a
    # property of the commission at the time it was earned -- changing the
    # platform's return window later must not retroactively move money that
    # was already promised. Same reasoning as `rate_percentage`.
    claimable_at = Column(DateTime(timezone=True), nullable=True)

    # How much of `order_subtotal` came back. Supports a PARTIAL reversal: a
    # customer returning one item out of five should cost the referrer the
    # commission on that item, not all of it -- the other four were delivered
    # and kept.
    returned_subtotal = Column(
        Numeric(10, 2), nullable=False, default=0, server_default="0"
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
    beneficiary_partner = relationship("Partner", foreign_keys=[beneficiary_partner_id])
    source_user = relationship("User", foreign_keys=[source_user_id])

    __table_args__ = (
        # Idempotency lives in two PARTIAL unique indexes created by migration
        # 027 — one per beneficiary type. A plain composite UNIQUE cannot do
        # the job here because NULLs never collide, so rows owed to a partner
        # (user id NULL) would not be deduplicated at all.
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
