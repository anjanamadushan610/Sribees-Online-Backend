"""
Partner SQLAlchemy Model — the professional referral team.

Deliberately a **separate identity store** from ``admins``. Partners are
outside contractors; administrators run the business. Keeping them in one
table would mean every admin route is one missing role check away from being
reachable by an agent, and role checks are exactly the thing that gets
forgotten on the next endpoint someone adds.

Two levels, and only two:

    Partner A recruits Partner B   (B.parent_partner_id = A)
    B signs up a customer          (users.referred_by_partner_id = B)
    That customer orders           -> B earns level 1, A earns level 2

``parent_partner_id`` is write-once for the same reason a customer's referrer
is: moving it retroactively changes who was owed money on orders already
recorded.
"""
import uuid

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    String,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from app.config.database import Base


class Partner(Base):
    """A professional referral agent."""

    __tablename__ = "partners"

    partner_id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    user_id = Column(UUID(as_uuid=True), ForeignKey("users.user_id", ondelete="RESTRICT"), nullable=True, unique=True, index=True)
    role = Column(String(20), nullable=False, default="master_partner", server_default="master_partner")
    email = Column(String(255), nullable=False, index=True)
    password_hash = Column(String(255), nullable=False)
    full_name = Column(String(150), nullable=False)
    phone = Column(String(20), nullable=True)

    referral_code = Column(
        String(12),
        nullable=True,
        index=True,
        comment="The code this partner hands out to customers and recruits.",
    )

    parent_partner_id = Column(
        UUID(as_uuid=True),
        ForeignKey("partners.partner_id", ondelete="SET NULL"),
        nullable=True,
        index=True,
        comment="Who recruited this partner. WRITE ONCE — level-2 commission "
                "history is attributed through it.",
    )

    is_active = Column(Boolean, default=True, nullable=False, server_default="true")
    last_login = Column(DateTime(timezone=True), nullable=True)
    password_changed_at = Column(
        DateTime(timezone=True),
        nullable=True,
        comment="Tokens issued before this are refused (get_current_partner).",
    )

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    parent = relationship("Partner", remote_side=[partner_id], backref="recruits")

    __table_args__ = (
        CheckConstraint(
            "parent_partner_id IS NULL OR parent_partner_id <> partner_id",
            name="chk_partner_no_self_parent",
        ),
    )

    def __repr__(self):
        return f"<Partner {self.email}>"
