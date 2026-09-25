"""
Notification SQLAlchemy Models
"""
import enum
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
from sqlalchemy.orm import relationship

from app.config.database import Base


class NotificationType(str, enum.Enum):
    """Notification types."""
    ORDER_STATUS = "order_status"
    PAYMENT = "payment"
    PROMOTION = "promotion"
    PRICE_DROP = "price_drop"
    SYSTEM = "system"
    DELIVERY = "delivery"


class Notification(Base):
    """User notification model."""

    __tablename__ = "notifications"

    notification_id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.user_id", ondelete="CASCADE"), nullable=False)

    type = Column(String(50), default=NotificationType.SYSTEM.value)
    title = Column(String(255), nullable=False)
    message = Column(Text, nullable=False)

    # Optional reference to related entity
    reference_type = Column(String(50), nullable=True)  # order, product, etc.
    reference_id = Column(UUID(as_uuid=True), nullable=True)

    # Additional data (JSON)
    data = Column(JSONB, nullable=True)

    # Status
    is_read = Column(Boolean, default=False)
    read_at = Column(DateTime(timezone=True), nullable=True)

    # Push notification
    push_sent = Column(Boolean, default=False)
    push_sent_at = Column(DateTime(timezone=True), nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())

    # Relationships
    user = relationship("User", back_populates="notifications")

    def __repr__(self):
        return f"<Notification {self.notification_id}>"


class PushToken(Base):
    """User push notification token (FCM device token)."""

    __tablename__ = "push_tokens"

    token_id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.user_id", ondelete="CASCADE"), nullable=False)
    token = Column(String(255), unique=True, nullable=False)
    device_type = Column(String(50), nullable=True)  # ios, android
    device_name = Column(String(100), nullable=True)
    # Stable per-install device identifier. Lets a device that rotated its FCM
    # token update its existing row instead of leaving a stale one behind.
    device_id = Column(String(255), nullable=True, index=True)
    # The language of the install behind this token ('en' | 'si' | 'ta'), sent
    # by the app whenever it registers the token or the user switches
    # language. A push is rendered per device in this language.
    language = Column(String(5), nullable=True)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    # Relationships
    user = relationship("User", back_populates="push_tokens")

    def __repr__(self):
        return f"<PushToken {self.token_id}>"


class CampaignCategory(str, enum.Enum):
    """The three switches a customer controls in the app (all default ON)."""
    OFFERS = "offers"
    PROMOTIONS = "promotions"
    GENERAL = "general"


class CampaignStatus(str, enum.Enum):
    SCHEDULED = "scheduled"
    SENDING = "sending"
    SENT = "sent"
    FAILED = "failed"
    CANCELLED = "cancelled"


class NotificationCampaign(Base):
    """
    A marketing push composed once in three languages (migration 071).

    ``title`` and ``message`` are ``{"en": ..., "si": ..., "ta": ...}``;
    English is required and stands in for a language left blank. The
    background worker (``campaign_worker``) delivers it at ``scheduled_at`` to
    every customer in the audience who has not switched ``category`` off.
    """

    __tablename__ = "notification_campaigns"

    campaign_id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    category = Column(String(20), nullable=False)
    title = Column(JSONB, nullable=False)
    message = Column(JSONB, nullable=False)
    image_url = Column(String(500), nullable=True)
    # Where a tap goes: 'product' (link_value = product id), 'offers', or
    # NULL for the notifications feed.
    link_type = Column(String(20), nullable=True)
    link_value = Column(String(100), nullable=True)
    # NULL = every customer (Super Admin only); otherwise the customers this
    # branch has served.
    branch_id = Column(
        UUID(as_uuid=True), ForeignKey("branches.branch_id", ondelete="SET NULL"), nullable=True
    )
    status = Column(String(20), nullable=False, default=CampaignStatus.SCHEDULED.value)
    scheduled_at = Column(DateTime(timezone=True), nullable=False)
    started_at = Column(DateTime(timezone=True), nullable=True)
    finished_at = Column(DateTime(timezone=True), nullable=True)
    cancelled_at = Column(DateTime(timezone=True), nullable=True)

    # Customers who got it in their in-app feed / devices it was pushed to.
    recipients = Column(Integer, nullable=False, default=0)
    devices = Column(Integer, nullable=False, default=0)
    delivered = Column(Integer, nullable=False, default=0)
    failed = Column(Integer, nullable=False, default=0)
    # Customers (not taps) who opened it, from the push or from the feed.
    opened = Column(Integer, nullable=False, default=0)
    error = Column(Text, nullable=True)

    created_by = Column(
        UUID(as_uuid=True), ForeignKey("admins.admin_id", ondelete="SET NULL"), nullable=True
    )
    created_by_name = Column(String(255), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    def __repr__(self):
        return f"<NotificationCampaign {self.campaign_id} {self.status}>"
