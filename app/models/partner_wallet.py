"""Linked partner money is separate from non-withdrawable shopping credit."""
import uuid
from sqlalchemy import Column, DateTime, ForeignKey, Numeric, String, Text, UniqueConstraint, CheckConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from app.config.database import Base


class PartnerApplication(Base):
    __tablename__ = "partner_applications"
    application_id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.user_id", ondelete="RESTRICT"), nullable=False, unique=True)
    password_hash = Column(String(255), nullable=True)
    status = Column(String(20), nullable=False, default="pending")
    decision_by = Column(UUID(as_uuid=True), nullable=True)
    decision_note = Column(String(500), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    decided_at = Column(DateTime(timezone=True), nullable=True)


class PartnerWallet(Base):
    __tablename__ = "partner_wallets"
    partner_id = Column(UUID(as_uuid=True), ForeignKey("partners.partner_id", ondelete="RESTRICT"), primary_key=True)
    balance = Column(Numeric(14, 2), nullable=False, default=0)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class PartnerWalletEntry(Base):
    __tablename__ = "partner_wallet_entries"
    entry_id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    partner_id = Column(UUID(as_uuid=True), ForeignKey("partner_wallets.partner_id", ondelete="RESTRICT"), nullable=False, index=True)
    reference = Column(String(160), nullable=False)
    kind = Column(String(40), nullable=False)
    amount = Column(Numeric(14, 2), nullable=False)  # signed, append-only
    balance_after = Column(Numeric(14, 2), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    __table_args__ = (UniqueConstraint("partner_id", "reference", name="uq_partner_wallet_reference"),)


class PartnerWithdrawal(Base):
    __tablename__ = "partner_withdrawals"
    withdrawal_id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    partner_id = Column(UUID(as_uuid=True), ForeignKey("partners.partner_id", ondelete="RESTRICT"), nullable=False, index=True)
    request_id = Column(UUID(as_uuid=True), nullable=False)
    amount = Column(Numeric(14, 2), nullable=False)
    status = Column(String(20), nullable=False, default="pending")
    bank_name = Column(String(120), nullable=False)
    branch_name = Column(String(120), nullable=False)
    account_name = Column(String(150), nullable=False)
    account_number = Column(String(40), nullable=False)
    decision_by = Column(UUID(as_uuid=True), nullable=True)
    payment_reference = Column(String(160), nullable=True)
    decision_note = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    decided_at = Column(DateTime(timezone=True), nullable=True)
    __table_args__ = (UniqueConstraint("partner_id", "request_id", name="uq_partner_withdrawal_request"), CheckConstraint("amount > 0", name="chk_partner_withdrawal_amount"))
