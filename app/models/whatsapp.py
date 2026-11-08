"""WhatsApp deduplication, conversation state and durable replies."""

import uuid

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID

from app.config.database import Base


class WhatsAppConversation(Base):
    __tablename__ = "whatsapp_conversations"

    phone = Column(String(20), primary_key=True)
    language = Column(String(2), nullable=False, default="en")
    awaiting_details = Column(Boolean, nullable=False, default=False)
    ticket_id = Column(
        UUID(as_uuid=True), ForeignKey("support_tickets.ticket_id", ondelete="SET NULL")
    )
    staff_active = Column(Boolean, nullable=False, default=False)
    last_message_at = Column(DateTime(timezone=True), nullable=False)


class WhatsAppReply(Base):
    __tablename__ = "whatsapp_replies"

    reply_id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    message_id = Column(String(255), unique=True, nullable=False)
    phone = Column(String(20), nullable=False)
    payload = Column(JSONB, nullable=True)
    inbound_text = Column(Text)
    inbound_type = Column(String(30))
    received_at = Column(DateTime(timezone=True))
    ticket_id = Column(
        UUID(as_uuid=True),
        ForeignKey("support_tickets.ticket_id", ondelete="SET NULL"),
        index=True,
    )
    admin_id = Column(
        UUID(as_uuid=True), ForeignKey("admins.admin_id", ondelete="SET NULL")
    )
    is_staff = Column(Boolean, nullable=False, default=False)
    provider_message_id = Column(String(255), unique=True)
    error_code = Column(String(30))
    # ignored rows also retain message_id to deduplicate messages during handoff.
    status = Column(String(20), nullable=False, default="pending", index=True)
    attempts = Column(Integer, nullable=False, default=0)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    next_attempt_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    created_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
