"""
App installs and their daily usage (migration 073).

One ``AppInstall`` per install of the customer app, kept current by the app's
heartbeat; one ``AppUsageDay`` per install per Asia/Colombo day. Read by the
Super Admin "App Users" page: who has the app open now, on which version, and
how much each install has used it since it was downloaded.
"""
from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    String,
    func,
)
from sqlalchemy.dialects.postgresql import UUID

from app.config.database import Base


class AppInstall(Base):
    """One install of the customer app, identified by its own device_id."""

    __tablename__ = "app_installs"
    __table_args__ = (
        CheckConstraint("platform IN ('android', 'ios')", name="ck_app_installs_platform"),
    )

    device_id = Column(String(80), primary_key=True)
    # Whoever was signed in at the last heartbeat; NULL = a guest.
    user_id = Column(
        UUID(as_uuid=True), ForeignKey("users.user_id", ondelete="SET NULL"), nullable=True, index=True
    )
    platform = Column(String(10), nullable=False, default="android")
    app_version = Column(String(40), nullable=True)
    build_number = Column(String(20), nullable=True)
    # The phone's own first-install time: the real download date.
    installed_at = Column(DateTime(timezone=True), nullable=True)
    first_seen_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    last_seen_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    # False once the app reported going to the background.
    is_foreground = Column(Boolean, nullable=False, default=False)


class AppUsageDay(Base):
    """Foreground time and opens for one install on one Asia/Colombo day."""

    __tablename__ = "app_usage_days"
    __table_args__ = (
        CheckConstraint(
            "foreground_seconds >= 0 AND opens >= 0", name="ck_app_usage_days_nonneg"
        ),
    )

    device_id = Column(
        String(80), ForeignKey("app_installs.device_id", ondelete="CASCADE"), primary_key=True
    )
    usage_date = Column(Date, primary_key=True)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.user_id", ondelete="SET NULL"), nullable=True)
    foreground_seconds = Column(Integer, nullable=False, default=0)
    opens = Column(Integer, nullable=False, default=0)
