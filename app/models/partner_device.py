"""
Phones signed in to the SRIBEES Partner app (migration 122).

Partners are their own identity store, separate from customers, so their push
tokens live here rather than in user_devices.
"""
import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID

from app.config.database import Base


def _now():
    return datetime.now(timezone.utc)


class PartnerDevice(Base):
    __tablename__ = "partner_devices"

    device_id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    partner_id = Column(
        UUID(as_uuid=True),
        ForeignKey("partners.partner_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    token = Column(String(512), nullable=False, unique=True)
    platform = Column(String(10), nullable=False, default="android", server_default="android")
    locale = Column(String(5), nullable=False, default="en", server_default="en")
    app_version = Column(String(40), nullable=True)
    is_active = Column(Boolean, nullable=False, default=True, server_default="true")
    created_at = Column(DateTime(timezone=True), nullable=False, default=_now)
    last_seen_at = Column(DateTime(timezone=True), nullable=False, default=_now)
