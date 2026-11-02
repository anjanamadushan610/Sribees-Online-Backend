"""Free delivery offers per branch and grants to chosen customers (migration 098)."""

import uuid

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID

from app.config.database import Base


class FreeDeliveryBranchOffer(Base):
    """A branch's offer to everyone it delivers to. No row means the offer is off."""

    __tablename__ = "free_delivery_branch_offers"
    branch_id = Column(UUID(as_uuid=True), ForeignKey("branches.branch_id", ondelete="CASCADE"), primary_key=True)
    enabled = Column(Boolean, nullable=False, default=False, server_default=text("false"))
    months = Column(Integer, nullable=False, default=3, server_default="3")
    min_order = Column(Numeric(10, 2), nullable=False, default=0, server_default="0")
    # The period is counted from here: each switch-on starts it again.
    enabled_at = Column(DateTime(timezone=True), nullable=True)
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())


class FreeDeliveryGrant(Base):
    """Free delivery a Super Admin gave one customer."""

    __tablename__ = "free_delivery_grants"
    grant_id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.user_id", ondelete="CASCADE"), nullable=False)
    # NULL: every branch.
    branch_id = Column(UUID(as_uuid=True), ForeignKey("branches.branch_id", ondelete="CASCADE"), nullable=True)
    starts_at = Column(DateTime(timezone=True), nullable=False)
    ends_at = Column(DateTime(timezone=True), nullable=False)
    min_order = Column(Numeric(10, 2), nullable=False, default=0, server_default="0")
    note = Column(String(200), nullable=True)
    granted_by = Column(UUID(as_uuid=True), nullable=True)
    revoked_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    __table_args__ = (Index("ix_free_delivery_grants_user", "user_id"),)
