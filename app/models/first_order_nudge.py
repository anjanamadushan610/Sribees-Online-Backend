"""First-order nudge bookkeeping (migration 114, docs/plans/first-order-nudge.md)."""

from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    SmallInteger,
    String,
    func,
)
from sqlalchemy.dialects.postgresql import UUID

from app.config.database import Base


class FirstOrderNudge(Base):
    """One "place your first order" push per customer, ever."""

    __tablename__ = "first_order_nudges"

    user_id = Column(
        UUID(as_uuid=True),
        ForeignKey("users.user_id", ondelete="CASCADE"),
        primary_key=True,
    )
    # Which copy went out: offer_min / offer / area / general.
    variant = Column(String(20), nullable=False)
    sent_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())


class FirstOrderNudgeState(Base):
    """A single row: customers who signed up before ``since`` are never nudged."""

    __tablename__ = "first_order_nudge_state"
    __table_args__ = (CheckConstraint("id = 1", name="first_order_nudge_state_id_check"),)

    id = Column(SmallInteger, primary_key=True)
    since = Column(DateTime(timezone=True), nullable=False)
