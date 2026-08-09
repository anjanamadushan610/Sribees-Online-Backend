"""
Stock Transfer Model

Branch-to-branch inventory transfer requests. See migration 033 for the
lifecycle: pending -> approved -> in_transit -> completed, or pending ->
cancelled.
"""
from datetime import datetime
from enum import Enum
from typing import Optional
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.config.database import Base


class TransferStatus(str, Enum):
    """Stock transfer lifecycle status."""
    PENDING = "pending"
    APPROVED = "approved"
    IN_TRANSIT = "in_transit"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class StockTransfer(Base):
    """A request to move stock of one product from one branch to another."""

    __tablename__ = "stock_transfers"

    transfer_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    from_branch_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("branches.branch_id", ondelete="RESTRICT"), nullable=False, index=True
    )
    to_branch_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("branches.branch_id", ondelete="RESTRICT"), nullable=False, index=True
    )
    product_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("products.product_id", ondelete="RESTRICT"), nullable=False, index=True
    )
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=TransferStatus.PENDING.value, index=True
    )
    requested_by_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("admins.admin_id", ondelete="RESTRICT"), nullable=False
    )
    approved_by_id: Mapped[Optional[UUID]] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("admins.admin_id", ondelete="SET NULL"), nullable=True
    )
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    requested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    approved_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    shipped_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        CheckConstraint("from_branch_id <> to_branch_id", name="chk_stock_transfer_branches_differ"),
    )

    def __repr__(self) -> str:
        return f"<StockTransfer {self.transfer_id} {self.from_branch_id}->{self.to_branch_id} ({self.status})>"
