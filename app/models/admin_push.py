"""
Staff devices and the new-order alert latch (migration 109).

Kept apart from the customers' ``push_tokens``: a staff phone belongs to an
admin, rings for orders, and must never receive customer marketing.
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
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID

from app.config.database import Base


class AdminPushToken(Base):
    """One staff device that wants new-order alerts."""

    __tablename__ = "admin_push_tokens"

    # Deploys run create_all before the SQL files, so this model -- not
    # migration 109 -- usually creates the table: names and defaults here
    # must match the migration exactly.
    token_id = Column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4,
        server_default=text("gen_random_uuid()"),
    )
    admin_id = Column(
        UUID(as_uuid=True),
        ForeignKey("admins.admin_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    token = Column(String(512), unique=True, nullable=False)
    device_id = Column(String(255), nullable=False, index=True)
    platform = Column(String(16), nullable=True)
    app_name = Column(String(32), nullable=False, default="new_orders", server_default="new_orders")
    language = Column(String(5), nullable=False, default="si", server_default="si")
    is_active = Column(Boolean, nullable=False, default=True, server_default="true")
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
    last_seen_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class AdminOrderAlert(Base):
    """Present once an order's staff alert has been sent (the send-once latch)."""

    __tablename__ = "admin_order_alerts"

    order_id = Column(
        UUID(as_uuid=True),
        ForeignKey("orders.order_id", ondelete="CASCADE"),
        primary_key=True,
    )
    sent_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    # Both NULL until the send finishes: a claimed alert that never completed
    # (FCM down, restart mid-send) stays visible as NULL rather than 0.
    devices = Column(Integer, nullable=True)
    recipients = Column(Integer, nullable=True)

    # Ring until accepted (migration 110). ``alert_status`` is the order's
    # status when it first rang: the order is "accepted" once it has moved on
    # from it (Confirm for COD; Packing for a card order that arrives
    # confirmed). ``rings`` counts within the current window, which restarts at
    # 07:00 Colombo for night orders. ``stopped_at`` set = never rings again.
    alert_status = Column(String(30), nullable=True)
    rings = Column(Integer, nullable=False, default=0, server_default="0")
    window_started_at = Column(DateTime(timezone=True), nullable=True)
    next_ring_at = Column(DateTime(timezone=True), nullable=True)
    stopped_at = Column(DateTime(timezone=True), nullable=True)
    # Set once the phones were told the order was accepted (clear push).
    cleared_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index(
            "ix_admin_order_alerts_next_ring_at",
            "next_ring_at",
            postgresql_where=text("stopped_at IS NULL"),
        ),
        Index(
            "ix_admin_order_alerts_uncleared",
            "order_id",
            postgresql_where=text("cleared_at IS NULL AND alert_status IS NOT NULL"),
        ),
    )
