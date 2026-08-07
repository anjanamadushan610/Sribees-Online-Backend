"""
Banner SQLAlchemy Model

Home-screen promotional banners. Branch-scoped by design: the carousel a
customer sees is the one their own branch's manager curated, so a Colombo
promotion never shows to a Galle shopper.

``branch_id`` NULL means "platform-wide" — a Super Admin banner every branch
shows. Branch Managers only ever create rows pinned to their own branch.
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
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from app.config.database import Base


class Banner(Base):
    """A single promotional slide in the mobile home-screen carousel."""

    __tablename__ = "banners"

    banner_id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    title = Column(String(120), nullable=False)
    subtitle = Column(String(200), nullable=True)

    # Stored as an origin-free relative path (see app/core/media.py); resolved
    # to a fetchable URL at serialization time.
    image_url = Column(String(500), nullable=True)

    # Optional destination when the customer taps "Shop Now". Null = the button
    # simply dismisses/scrolls, which is what a purely informational banner wants.
    link_type = Column(
        String(20),
        nullable=True,
        comment="What target_id points at: 'category' | 'product' | None",
    )
    target_id = Column(UUID(as_uuid=True), nullable=True)

    branch_id = Column(
        UUID(as_uuid=True),
        ForeignKey("branches.branch_id", ondelete="CASCADE"),
        nullable=True,
        index=True,
        comment="NULL = platform-wide banner shown in every branch",
    )

    sort_order = Column(
        Integer,
        default=0,
        nullable=False,
        server_default="0",
        comment="Ascending; controls carousel order",
    )
    is_active = Column(Boolean, default=True, nullable=False, server_default="true")

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    branch = relationship("Branch", backref="banners")

    __table_args__ = (
        # The storefront's only read pattern: active banners for one branch
        # (plus the platform-wide ones), in display order.
        Index("idx_banners_branch_active_order", "branch_id", "is_active", "sort_order"),
    )

    def __repr__(self):
        return f"<Banner {self.title}>"
