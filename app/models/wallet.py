"""
Wallet & Cashback SQLAlchemy Models

- `Wallet`      : one row per user holding the current cashback balance.
- `WalletTransaction`: append-only ledger of earned/spent movements.
"""
import enum
import uuid

from sqlalchemy import (
    Column,
    DateTime,
    ForeignKey,
    Numeric,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from app.config.database import Base


class WalletTransactionType(str, enum.Enum):
    """Direction of a wallet movement."""
    EARNED = "earned"   # cashback credited to the wallet
    SPENT = "spent"     # balance used to pay for an order
    REFUND = "refund"   # balance returned on a cancelled order


class Wallet(Base):
    """User wallet holding the cashback balance (currency: LKR)."""

    __tablename__ = "wallets"

    wallet_id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(
        UUID(as_uuid=True),
        ForeignKey("users.user_id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    balance = Column(Numeric(10, 2), nullable=False, default=0)
    currency = Column(String(3), nullable=False, default="LKR")

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )

    # Relationships
    user = relationship("User", back_populates="wallet")
    transactions = relationship(
        "WalletTransaction",
        back_populates="wallet",
        cascade="all, delete-orphan",
        order_by="desc(WalletTransaction.created_at)",
    )

    def __repr__(self):
        return f"<Wallet user={self.user_id} balance={self.balance}>"


class WalletTransaction(Base):
    """A single earned/spent movement on a wallet (append-only ledger)."""

    __tablename__ = "wallet_transactions"

    transaction_id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    wallet_id = Column(
        UUID(as_uuid=True),
        ForeignKey("wallets.wallet_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id = Column(
        UUID(as_uuid=True),
        ForeignKey("users.user_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    order_id = Column(
        UUID(as_uuid=True),
        ForeignKey("orders.order_id", ondelete="SET NULL"),
        nullable=True,
    )

    # 'earned' | 'spent' | 'refund'
    type = Column(String(20), nullable=False)
    title = Column(String(255), nullable=False)
    # Always a positive amount; `type` carries the direction.
    amount = Column(Numeric(10, 2), nullable=False)
    # Wallet balance immediately after this transaction was applied.
    balance_after = Column(Numeric(10, 2), nullable=True)

    order_number = Column(String(50), nullable=True)
    notes = Column(Text, nullable=True)
    # Why it moved (app.services.finance_outbox.WalletReason); NULL before 093.
    reason = Column(String(40), nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())

    # Relationships
    wallet = relationship("Wallet", back_populates="transactions")
    order = relationship("Order")

    def __repr__(self):
        return f"<WalletTransaction {self.type} {self.amount}>"
