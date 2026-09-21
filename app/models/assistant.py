"""Explicit customer preferences and individually opted-in replenishment reminders."""

import uuid

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID

from app.config.database import Base


class AssistantProfile(Base):
    __tablename__ = "assistant_profiles"
    user_id = Column(
        UUID(as_uuid=True),
        ForeignKey("users.user_id", ondelete="CASCADE"),
        primary_key=True,
    )
    preferences = Column(JSONB, nullable=False, default=dict, server_default="{}")
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class AssistantReminder(Base):
    __tablename__ = "assistant_reminders"
    reminder_id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(
        UUID(as_uuid=True),
        ForeignKey("users.user_id", ondelete="CASCADE"),
        nullable=False,
    )
    product_id = Column(
        UUID(as_uuid=True),
        ForeignKey("products.product_id", ondelete="CASCADE"),
        nullable=False,
    )
    product_name = Column(String(255), nullable=False)
    interval_days = Column(Integer, nullable=False)
    language = Column(String(2), nullable=False, default="si")
    active = Column(Boolean, nullable=False, default=True, server_default="true")
    next_due_at = Column(DateTime(timezone=True), nullable=False)
    last_sent_at = Column(DateTime(timezone=True))
    __table_args__ = (
        Index("ix_assistant_reminders_due", "active", "next_due_at"),
        Index("ix_assistant_reminders_user", "user_id"),
        Index(
            "uq_assistant_reminders_active_product",
            "user_id",
            "product_id",
            unique=True,
            postgresql_where=text("active"),
        ),
    )


class AssistantReceipt(Base):
    __tablename__ = "assistant_receipts"
    token = Column(UUID(as_uuid=True), primary_key=True)
    user_id = Column(
        UUID(as_uuid=True),
        ForeignKey("users.user_id", ondelete="CASCADE"),
        nullable=False,
    )
    response = Column(JSONB, nullable=False)
    created_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
