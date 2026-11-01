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


class AssistantChatLog(Base):
    """
    One chat turn in full, and what the customer did next (migration 097).
    Unlike ``assistant_turns`` this names the customer and keeps the real
    words, so it stays out of every customer-facing API.
    """

    __tablename__ = "assistant_chat_log"
    log_id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.user_id", ondelete="SET NULL"), nullable=True)
    session_id = Column(String(200), nullable=False)
    branch_id = Column(UUID(as_uuid=True), nullable=True)
    language = Column(String(2), nullable=True)
    answered_by = Column(String(12), nullable=True)
    message = Column(Text, nullable=False)
    reply = Column(Text, nullable=False)
    had_image = Column(Boolean, nullable=False, default=False, server_default=text("false"))
    product_ids = Column(JSONB, nullable=False, default=list, server_default="[]")
    added_to_cart = Column(JSONB, nullable=False, default=list, server_default="[]")
    ordered = Column(JSONB, nullable=False, default=list, server_default="[]")
    __table_args__ = (
        Index("ix_assistant_chat_log_user", "user_id", "created_at"),
        Index("ix_assistant_chat_log_created", "created_at"),
    )


class AssistantPattern(Base):
    """A message plan learned from the model (migration 090); see hybrid/memory.py."""

    __tablename__ = "assistant_patterns"
    pattern_id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    text_key = Column(Text, nullable=False, unique=True)
    plan = Column(JSONB, nullable=False)
    status = Column(String(12), nullable=False, default="shadow", server_default="shadow")
    source = Column(String(20), nullable=False, default="model", server_default="model")
    examples = Column(Integer, nullable=False, default=0, server_default="0")
    shadow_agree = Column(Integer, nullable=False, default=0, server_default="0")
    shadow_disagree = Column(Integer, nullable=False, default=0, server_default="0")
    canary_agree = Column(Integer, nullable=False, default=0, server_default="0")
    canary_disagree = Column(Integer, nullable=False, default=0, server_default="0")
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())
    promoted_at = Column(DateTime(timezone=True), nullable=True)
    __table_args__ = (Index("ix_assistant_patterns_status", "status"),)


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
