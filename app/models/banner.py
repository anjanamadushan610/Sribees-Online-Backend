"""
Banner SQLAlchemy Model

Home-screen promotional banners. Branch-scoped by design: the carousel a
customer sees is the one their own branch's manager curated, so a Colombo
promotion never shows to a Galle shopper.

``branch_id`` NULL means "platform-wide" — a Super Admin banner every branch
shows. Branch Managers only ever create rows pinned to their own branch.
"""
import enum
import uuid

from sqlalchemy import (
    Boolean,
    CheckConstraint,
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
        comment=(
            "Tap destination: 'category' | 'product' | 'quick_sale' | None. "
            "'quick_sale' is a fixed screen and carries no target_id."
        ),
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

    # Scheduling. Both NULL = always active, which is every banner created
    # before this existed. The STOREFRONT query filters on these, so a campaign
    # banner disappears from customers' phones the moment it expires rather
    # than waiting for someone to remember to toggle it off. `is_active` stays
    # the manual switch; these are the automatic one.
    starts_at = Column(DateTime(timezone=True), nullable=True)
    ends_at = Column(DateTime(timezone=True), nullable=True)

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

    def schedule_status(self, now=None) -> str:
        """
        'live' | 'scheduled' | 'expired' | 'inactive', for the admin table.

        Distinguishes the two ways a banner can be off a customer's screen:
        'inactive' is a person having switched it off, 'expired' is its own
        schedule having run out. Conflating them makes an admin re-enable a
        banner whose end date has simply passed.
        """
        from datetime import datetime, timezone

        if not self.is_active:
            return "inactive"
        now = now or datetime.now(timezone.utc)
        if self.starts_at is not None and now < self.starts_at:
            return "scheduled"
        if self.ends_at is not None and now > self.ends_at:
            return "expired"
        return "live"

    def __repr__(self):
        return f"<Banner {self.title}>"


class BannerEventType(str, enum.Enum):
    """What a :class:`BannerEvent` row records."""

    IMPRESSION = "impression"  # the slide was actually rendered on screen
    CLICK = "click"            # the customer tapped through to its target


class BannerEvent(Base):
    """
    One impression or click on a banner.

    Stored as an event log rather than two counter columns on ``banners``
    because the only actionable form of CTR is windowed ("last 7 days"), and a
    counter can only ever answer "since forever" — a banner that performed well
    in July would keep looking healthy in September.

    ``branch_id`` is the branch whose storefront displayed the slide, so a
    platform-wide banner (``Banner.branch_id IS NULL``) still reports a
    per-branch CTR, which is the number a branch's Marketing Manager can act on.
    """

    __tablename__ = "banner_events"

    event_id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    banner_id = Column(
        UUID(as_uuid=True),
        ForeignKey("banners.banner_id", ondelete="CASCADE"),
        nullable=False,
    )
    event_type = Column(String(20), nullable=False)
    branch_id = Column(
        UUID(as_uuid=True),
        ForeignKey("branches.branch_id", ondelete="SET NULL"),
        nullable=True,
    )
    # NULL for a guest shopper. Never joined for display — it exists so a
    # signed-in customer's repeat views can be collapsed, nothing else.
    user_id = Column(
        UUID(as_uuid=True),
        ForeignKey("users.user_id", ondelete="SET NULL"),
        nullable=True,
    )
    # Per-device dedupe key supplied by the client. Combined with the partial
    # unique index below, it caps a session at one impression per banner per
    # hour: without it, a customer scrolling the home screen up and down
    # inflates impressions and craters every CTR on the dashboard.
    session_key = Column(String(64), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    banner = relationship("Banner", backref="events")

    __table_args__ = (
        CheckConstraint(
            "event_type IN ('impression', 'click')",
            name="ck_banner_events_type",
        ),
        # The dashboard's only read pattern: events for a set of banners,
        # in one branch, since a cutoff.
        Index("idx_banner_events_banner_created", "banner_id", "created_at"),
        Index("idx_banner_events_branch_created", "branch_id", "created_at"),
        # One impression per banner per session per hour. This is what stops a
        # customer scrolling the home screen up and down from inflating
        # impressions and dragging every CTR below what the banner really
        # earned -- the failure mode is silent, so it has to be the database
        # that enforces it rather than the endpoint.
        #
        # Declared here as well as in migration 048 because a table built by
        # create_all() (fresh deploy, E2E harness, new developer) would
        # otherwise be missing it and would over-count.
        #
        # `created_at AT TIME ZONE 'UTC'` makes date_trunc IMMUTABLE, which an
        # index expression requires; date_trunc on a timestamptz is only STABLE.
        Index(
            "uq_banner_events_impression_dedupe",
            "banner_id",
            "session_key",
            text("date_trunc('hour', created_at AT TIME ZONE 'UTC')"),
            unique=True,
            postgresql_where=text(
                "event_type = 'impression' AND session_key IS NOT NULL"
            ),
        ),
    )

    def __repr__(self):
        return f"<BannerEvent {self.event_type} banner={self.banner_id}>"
