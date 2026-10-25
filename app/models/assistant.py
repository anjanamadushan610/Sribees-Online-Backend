"""
Explicit customer preferences, individually opted-in replenishment reminders,
and the branch briefings the assistant reads before it answers.
"""

import uuid

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID

from app.config.database import Base


class AssistantTurn(Base):
    """
    One chat turn as the hybrid assistant's learner sees it (migration 089).
    No personal data: ``text_key`` is the scrubbed, folded search key of the
    message and ``person_hash`` a salted hash -- see hybrid/recorder.py.
    """

    __tablename__ = "assistant_turns"
    turn_id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    branch_id = Column(UUID(as_uuid=True), nullable=True)
    person_hash = Column(String(32), nullable=True)
    language = Column(String(2), nullable=True)
    text_key = Column(Text, nullable=False)
    signature = Column(String(200), nullable=True)
    layer = Column(String(12), nullable=False)
    plan = Column(JSONB, nullable=False, default=dict, server_default="{}")
    shadow_plan = Column(JSONB, nullable=True)
    model_trace = Column(JSONB, nullable=True)
    reply_kind = Column(String(40), nullable=True)
    latency_ms = Column(Integer, nullable=True)
    model_calls = Column(Integer, nullable=False, default=0, server_default="0")
    prompt_tokens = Column(Integer, nullable=False, default=0, server_default="0")
    output_tokens = Column(Integer, nullable=False, default=0, server_default="0")
    outcome = Column(JSONB, nullable=False, default=dict, server_default="{}")
    __table_args__ = (
        Index("ix_assistant_turns_created", "created_at"),
        Index("ix_assistant_turns_signature", "signature"),
        Index("ix_assistant_turns_layer", "layer", "created_at"),
    )


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


class AssistantBriefing(Base):
    """
    Something a branch wants the assistant to know for a while -- see
    migrations/072_assistant_briefings.sql. ``digest`` is what the model
    reads; ``source_text`` is what the manager uploaded.
    """

    __tablename__ = "assistant_briefings"
    briefing_id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    branch_id = Column(
        UUID(as_uuid=True),
        ForeignKey("branches.branch_id", ondelete="CASCADE"),
        nullable=True,
    )
    title = Column(String(120), nullable=False)
    source_filename = Column(String(255))
    source_text = Column(Text, nullable=False)
    digest = Column(Text, nullable=False)
    digest_source = Column(String(10), nullable=False, default="ai")
    mode = Column(String(10), nullable=False, default="relevant")
    starts_at = Column(DateTime(timezone=True), nullable=False)
    ends_at = Column(DateTime(timezone=True), nullable=False)
    is_active = Column(Boolean, nullable=False, default=True, server_default="true")
    created_by = Column(
        UUID(as_uuid=True), ForeignKey("admins.admin_id", ondelete="SET NULL")
    )
    updated_by = Column(
        UUID(as_uuid=True), ForeignKey("admins.admin_id", ondelete="SET NULL")
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
    __table_args__ = (
        Index(
            "ix_assistant_briefings_branch_window", "branch_id", "ends_at", "starts_at"
        ),
    )
