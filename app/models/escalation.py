"""
Order Escalation model (QA spec B4, Module B).

An internal support -> branch ticket to intercept an order before dispatch
(urgent cancellation, address correction, hold, customer complaint). Raised by
Customer Support / Branch Manager / Super Admin; acknowledged and resolved by
the Branch Manager (own branch) or Super Admin.
"""
import enum
import uuid

from sqlalchemy import Column, DateTime, ForeignKey, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from app.config.database import Base


class EscalationCategory(str, enum.Enum):
    CANCEL_REQUEST = "cancel_request"
    ADDRESS_CORRECTION = "address_correction"
    HOLD_SHIPMENT = "hold_shipment"
    CUSTOMER_COMPLAINT = "customer_complaint"
    OTHER = "other"


class EscalationStatus(str, enum.Enum):
    OPEN = "open"
    ACKNOWLEDGED = "acknowledged"
    RESOLVED = "resolved"


class OrderEscalation(Base):
    __tablename__ = "order_escalations"

    escalation_id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    order_id = Column(
        UUID(as_uuid=True),
        ForeignKey("orders.order_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # Snapshot of the fulfilling branch so a branch reassignment on the order
    # never moves an old ticket out of a manager's queue.
    branch_id = Column(UUID(as_uuid=True), nullable=True, index=True)

    raised_by_admin_id = Column(
        UUID(as_uuid=True),
        ForeignKey("admins.admin_id", ondelete="SET NULL"),
        nullable=True,
    )
    raised_by_name = Column(String(255), nullable=False)
    raised_by_role = Column(String(50), nullable=True)

    category = Column(String(30), nullable=False, default=EscalationCategory.OTHER.value)
    message = Column(Text, nullable=False)

    status = Column(String(20), nullable=False, default=EscalationStatus.OPEN.value, index=True)
    resolution_note = Column(Text, nullable=True)
    handled_by_name = Column(String(255), nullable=True)

    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    acknowledged_at = Column(DateTime(timezone=True), nullable=True)
    resolved_at = Column(DateTime(timezone=True), nullable=True)

    order = relationship("Order")

    def __repr__(self) -> str:
        return f"<OrderEscalation {self.escalation_id} {self.category}/{self.status}>"
