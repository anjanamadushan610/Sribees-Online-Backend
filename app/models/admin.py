"""
Admin User Model
"""
from datetime import datetime
from enum import Enum
from typing import Optional
from uuid import UUID, uuid4

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, String, func
from sqlalchemy import Enum as SQLEnum
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.config.database import Base


class AdminRole(str, Enum):
    """Admin role types."""
    SUPER_ADMIN = "super_admin"
    BRANCH_MANAGER = "branch_manager"
    MARKETING_MANAGER = "marketing_manager"
    INVENTORY_MANAGER = "inventory_manager"
    CUSTOMER_SUPPORT = "customer_support"
    STAFF = "staff"


class Admin(Base):
    """Admin user model for admin panel access."""

    __tablename__ = "admins"
    __table_args__ = (
        CheckConstraint(
            "parent_admin_id IS NULL OR parent_admin_id <> admin_id",
            name="chk_admin_no_self_parent",
        ),
    )

    admin_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
    )
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    full_name: Mapped[str] = mapped_column(String(100), nullable=False)
    role: Mapped[AdminRole] = mapped_column(
        SQLEnum(AdminRole, name="admin_role_enum"),
        nullable=False,
        default=AdminRole.CUSTOMER_SUPPORT,
    )
    # Points at the admin's row in `roles`: a system role for the 5 base roles,
    # or a custom delegated role for staff (role='staff'). SET NULL (rather
    # than blocking role deletion) is intentional — see Role's docstring.
    role_id: Mapped[Optional[UUID]] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("roles.role_id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    # Set only for staff rows (role='staff'): the base-role admin who created
    # this account and whose permission set bounds it. Hierarchy is capped at
    # one level, so this single column covers both "who created this" and
    # "whose subordinate is this" — no separate created_by column needed.
    parent_admin_id: Mapped[Optional[UUID]] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("admins.admin_id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    branch_id: Mapped[Optional[UUID]] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    last_login: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    def __repr__(self) -> str:
        return f"<Admin {self.email} ({self.role})>"


class AdminSession(Base):
    """Admin session for JWT token management."""

    __tablename__ = "admin_sessions"

    session_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
    )
    admin_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("admins.admin_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    refresh_token_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    device_info: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    ip_address: Mapped[Optional[str]] = mapped_column(String(45), nullable=True)
    user_agent: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    # Relationships
    admin: Mapped["Admin"] = relationship("Admin", backref="sessions")
