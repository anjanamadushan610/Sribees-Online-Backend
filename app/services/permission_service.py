"""
Permission Service

Resolves an admin's effective (resource, action) permission set from the DB
(role_id -> role_permissions -> permissions) and validates the "subset of the
creator's own permissions, and never an excluded resource" rule that governs
staff/role creation.
"""
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permission_catalog import EXCLUDED_RESOURCES, SYSTEM_ROLE_PERMISSIONS
from app.models.admin import Admin
from app.models.role import Permission, RolePermission


class PermissionService:
    """Effective-permission resolution and delegation-safety checks."""

    @staticmethod
    async def get_effective_permissions(db: AsyncSession, admin: Admin) -> set[tuple[str, str]]:
        """Concrete (resource, action) set the admin currently holds."""
        if admin.role_id is None:
            # Defensive only — every admin gets a role_id at creation time
            # post-migration. Falls back to the static system map by role.
            role_value = admin.role.value if hasattr(admin.role, "value") else admin.role
            return set(SYSTEM_ROLE_PERMISSIONS.get(role_value, set()))

        rows = (
            await db.execute(
                select(Permission.resource, Permission.action)
                .join(RolePermission, RolePermission.permission_id == Permission.permission_id)
                .where(RolePermission.role_id == admin.role_id)
            )
        ).all()
        return set(rows)

    @staticmethod
    async def get_effective_permission_ids(db: AsyncSession, admin: Admin) -> set[UUID]:
        if admin.role_id is None:
            return set()
        rows = (
            await db.execute(
                select(RolePermission.permission_id).where(RolePermission.role_id == admin.role_id)
            )
        ).scalars().all()
        return set(rows)

    @staticmethod
    async def resolve_permissions(db: AsyncSession, permission_ids: set[UUID]) -> list[Permission]:
        """Look up Permission rows for a set of ids; raises 400 on unknown ids."""
        if not permission_ids:
            return []
        rows = (
            await db.execute(select(Permission).where(Permission.permission_id.in_(permission_ids)))
        ).scalars().all()
        found_ids = {p.permission_id for p in rows}
        missing = permission_ids - found_ids
        if missing:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Unknown permission id(s): {', '.join(str(m) for m in missing)}",
            )
        return list(rows)

    @staticmethod
    async def validate_delegation(
        db: AsyncSession, creator: Admin, requested_ids: set[UUID],
    ) -> list[Permission]:
        """
        Raises 400 unless every requested permission is (a) a real permission,
        (b) held by the creator right now, and (c) not on an excluded
        resource. Returns the resolved Permission rows on success.
        """
        if not requested_ids:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="A role must have at least one permission.",
            )

        permissions = await PermissionService.resolve_permissions(db, requested_ids)

        excluded = [p for p in permissions if p.resource in EXCLUDED_RESOURCES]
        if excluded:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    f"'{excluded[0].resource}' permissions cannot be delegated to a staff role."
                ),
            )

        creator_ids = await PermissionService.get_effective_permission_ids(db, creator)
        extra = requested_ids - creator_ids
        if extra:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Cannot grant {len(extra)} permission(s) you do not hold yourself.",
            )

        return permissions
