"""
Dynamic Role / Permission Models

Backs the delegated staff system: any base-role admin can define a
custom-named role (e.g. "Branch Manager Staff") scoped to a subset of their
own effective permissions, then create a staff account holding that role.

`permissions` rows are always concrete (resource, action) pairs — never
wildcards. Wildcard shorthand (`{resource:'*', action:'*'}`) is an authoring
convenience that exists only on the frontend's static ROLE_PERMISSIONS map;
at seed time every wildcard is expanded into concrete rows here, so "is set A
a subset of set B" stays a plain set operation everywhere it matters for
authorization, with no wildcard-matching logic to get wrong in a security
path.
"""
from datetime import datetime
from typing import Optional
from uuid import UUID, uuid4

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.config.database import Base


class Role(Base):
    """
    A named, permission-bearing role.

    The 5 pre-seeded system roles (`is_system=True`, `system_key` set) back
    the original fixed AdminRole enum values. Every other row is a custom
    role a base-role admin created for their own staff (`created_by_admin_id`
    set, `is_system=False`).

    No role-delete endpoint exists yet. If one is added later: deleting a
    role SET NULLs `admins.role_id` for any admin still pointing at it, which
    PermissionService resolves to an empty effective-permission set (safe by
    default) rather than silently inheriting anything.
    """

    __tablename__ = "roles"

    role_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    is_system: Mapped[bool] = mapped_column(default=False, nullable=False)
    system_key: Mapped[Optional[str]] = mapped_column(String(30), unique=True, nullable=True)
    created_by_admin_id: Mapped[Optional[UUID]] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("admins.admin_id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False,
    )

    permissions: Mapped[list["Permission"]] = relationship(
        "Permission", secondary="role_permissions", backref="roles",
    )

    def __repr__(self) -> str:
        return f"<Role {self.name} ({'system' if self.is_system else 'custom'})>"


class Permission(Base):
    """A single (resource, action) grant. The full catalog of what can ever be delegated."""

    __tablename__ = "permissions"
    __table_args__ = (
        UniqueConstraint("resource", "action", name="uq_permissions_resource_action"),
    )

    permission_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    resource: Mapped[str] = mapped_column(String(50), nullable=False)
    action: Mapped[str] = mapped_column(String(20), nullable=False)

    def __repr__(self) -> str:
        return f"<Permission {self.resource}:{self.action}>"


class RolePermission(Base):
    """Join row: role X holds permission Y."""

    __tablename__ = "role_permissions"

    role_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("roles.role_id", ondelete="CASCADE"), primary_key=True,
    )
    permission_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("permissions.permission_id", ondelete="CASCADE"), primary_key=True,
    )
