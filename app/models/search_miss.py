"""
Searches that found nothing — the inbox admins work through to teach search
the words shoppers actually use.
"""
from sqlalchemy import Column, DateTime, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import UUID

from app.config.database import Base


class SearchMissStatus:
    OPEN = "open"
    RESOLVED = "resolved"   # attached to a product as a search keyword
    IGNORED = "ignored"     # noise, or something we will never sell

    ALL = (OPEN, RESOLVED, IGNORED)


class SearchMiss(Base):
    """
    One zero-result search, aggregated by what it means rather than how it
    was typed: "Seeni", "seeni" and "සීනි" share ``query_key`` (the folded
    words from app.services.search_text, run together), so an admin sees
    one row.

    Holds no shopper identity. ``shoppers`` counts distinct shoppers per day
    (deduplicated in Redis), so one person retrying ten times is one.
    """

    __tablename__ = "search_misses"

    query_key = Column(String(200), primary_key=True)
    sample_query = Column(Text, nullable=False, comment="The latest spelling a shopper typed")
    shoppers = Column(Integer, nullable=False, default=1, server_default="1")
    first_seen = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    last_seen = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    last_branch_id = Column(
        UUID(as_uuid=True),
        ForeignKey("branches.branch_id", ondelete="SET NULL"),
        nullable=True,
    )
    status = Column(String(16), nullable=False, default=SearchMissStatus.OPEN,
                    server_default=SearchMissStatus.OPEN)
    resolved_product_id = Column(
        UUID(as_uuid=True),
        ForeignKey("products.product_id", ondelete="SET NULL"),
        nullable=True,
    )
    resolved_by = Column(
        UUID(as_uuid=True),
        ForeignKey("admins.admin_id", ondelete="SET NULL"),
        nullable=True,
    )
    resolved_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index("idx_search_misses_inbox", "status", "last_seen"),
    )

    def __repr__(self):
        return f"<SearchMiss {self.query_key!r} x{self.shoppers}>"
