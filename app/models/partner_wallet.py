"""Linked partner money is separate from non-withdrawable shopping credit."""
import uuid

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID as PG_UUID

from app.config.database import Base


class PartnerApplication(Base):
    __tablename__ = "partner_applications"
    application_id = sa.Column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = sa.Column(PG_UUID(as_uuid=True), sa.ForeignKey("users.user_id", ondelete="RESTRICT"), nullable=False, unique=True)
    password_hash = sa.Column(sa.String(255), nullable=True)
    status = sa.Column(sa.String(20), nullable=False, default="pending")
    decision_by = sa.Column(PG_UUID(as_uuid=True), nullable=True)
    decision_note = sa.Column(sa.String(500), nullable=True)
    created_at = sa.Column(sa.DateTime(timezone=True), server_default=sa.func.now())
    decided_at = sa.Column(sa.DateTime(timezone=True), nullable=True)


class PartnerWallet(Base):
    __tablename__ = "partner_wallets"
    partner_id = sa.Column(PG_UUID(as_uuid=True), sa.ForeignKey("partners.partner_id", ondelete="RESTRICT"), primary_key=True)
    balance = sa.Column(sa.Numeric(14, 2), nullable=False, default=0)
    updated_at = sa.Column(sa.DateTime(timezone=True), server_default=sa.func.now(), onupdate=sa.func.now())


class PartnerWalletEntry(Base):
    __tablename__ = "partner_wallet_entries"
    entry_id = sa.Column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    partner_id = sa.Column(PG_UUID(as_uuid=True), sa.ForeignKey("partner_wallets.partner_id", ondelete="RESTRICT"), nullable=False, index=True)
    reference = sa.Column(sa.String(160), nullable=False)
    kind = sa.Column(sa.String(40), nullable=False)
    amount = sa.Column(sa.Numeric(14, 2), nullable=False)  # signed, append-only
    balance_after = sa.Column(sa.Numeric(14, 2), nullable=False)
    created_at = sa.Column(sa.DateTime(timezone=True), server_default=sa.func.now())
    __table_args__ = (sa.UniqueConstraint("partner_id", "reference", name="uq_partner_wallet_reference"),)


class PartnerWithdrawal(Base):
    __tablename__ = "partner_withdrawals"
    withdrawal_id = sa.Column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    partner_id = sa.Column(PG_UUID(as_uuid=True), sa.ForeignKey("partners.partner_id", ondelete="RESTRICT"), nullable=False, index=True)
    request_id = sa.Column(PG_UUID(as_uuid=True), nullable=False)
    amount = sa.Column(sa.Numeric(14, 2), nullable=False)
    status = sa.Column(sa.String(20), nullable=False, default="pending")
    bank_name = sa.Column(sa.String(120), nullable=False)
    branch_name = sa.Column(sa.String(120), nullable=False)
    account_name = sa.Column(sa.String(150), nullable=False)
    account_number = sa.Column(sa.String(40), nullable=False)
    decision_by = sa.Column(PG_UUID(as_uuid=True), nullable=True)
    payment_reference = sa.Column(sa.String(160), nullable=True)
    decision_note = sa.Column(sa.Text, nullable=True)
    created_at = sa.Column(sa.DateTime(timezone=True), server_default=sa.func.now())
    decided_at = sa.Column(sa.DateTime(timezone=True), nullable=True)
    __table_args__ = (sa.UniqueConstraint("partner_id", "request_id", name="uq_partner_withdrawal_request"), sa.CheckConstraint("amount > 0", name="chk_partner_withdrawal_amount"))
