"""
Customer activity log (migration 121).

One ``ActivityEvent`` per thing a shopper did in the customer app or on the
web store: a screen, a product opened, an image swiped, a search, a cart
change. Written in batches by POST /app/activity/batch, read by Super Admin
as a timeline. Kept forever; deleted with a closed account.
"""
from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    String,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID

from app.config.database import Base


class ActivityEvent(Base):
    __tablename__ = "activity_events"
    __table_args__ = (
        CheckConstraint("platform IN ('android', 'ios', 'web')", name="ck_activity_events_platform"),
    )

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    # Indexes live in migration 121 (partial and BRIN ones the ORM cannot say).
    device_id = Column(String(80), nullable=False)
    # NULL = a guest. Set from the token, never from the request body.
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.user_id", ondelete="CASCADE"), nullable=True)
    platform = Column(String(10), nullable=False)
    app_version = Column(String(40), nullable=True)
    session_id = Column(String(40), nullable=True)
    event = Column(String(30), nullable=False)
    screen = Column(String(40), nullable=True)
    product_id = Column(UUID(as_uuid=True), nullable=True)
    category_id = Column(UUID(as_uuid=True), nullable=True)
    props = Column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    occurred_at = Column(DateTime(timezone=True), nullable=False)
    received_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
