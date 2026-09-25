"""
Customer Support Ticket model.

Support requests / inquiries submitted by customers through the mobile app's
Help & Support screen. Scoped to branches for Branch Managers and Customer Support,
visible system-wide to Super Admins.
"""
import enum
import uuid

from sqlalchemy import Column, DateTime, ForeignKey, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from app.config.database import Base


class TicketStatus(str, enum.Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    RESOLVED = "resolved"
    CLOSED = "closed"


class SupportTicket(Base):
    __tablename__ = "support_tickets"

    ticket_id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    ticket_number = Column(String(30), unique=True, nullable=False, index=True)

    user_id = Column(
        UUID(as_uuid=True),
        ForeignKey("users.user_id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    branch_id = Column(
        UUID(as_uuid=True),
        ForeignKey("branches.branch_id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    contact_phone = Column(String(30), nullable=False, index=True)
    customer_name = Column(String(255), nullable=True)
    # The install that filed a guest ticket (X-Device-Id). A guest lookup needs
    # it as well as the phone -- a phone number alone is not proof. See
    # migrations/070_support_ticket_device.sql.
    device_id = Column(String(128), nullable=True, index=True)
    category = Column(String(50), nullable=False, default="general")
    message = Column(Text, nullable=False)

    status = Column(String(30), nullable=False, default=TicketStatus.PENDING.value, index=True)
    assigned_admin_id = Column(
        UUID(as_uuid=True),
        ForeignKey("admins.admin_id", ondelete="SET NULL"),
        nullable=True,
    )
    resolution_notes = Column(Text, nullable=True)

    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now(), index=True)
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())
    resolved_at = Column(DateTime(timezone=True), nullable=True)

    # Relationships
    user = relationship("User", foreign_keys=[user_id], lazy="joined")
    branch = relationship("Branch", foreign_keys=[branch_id], lazy="joined")
    assigned_admin = relationship("Admin", foreign_keys=[assigned_admin_id], lazy="joined")
