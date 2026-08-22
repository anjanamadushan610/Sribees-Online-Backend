"""
Admin User Management API

CRUD for admin accounts (the ``admins`` table). Restricted to Super Admins
(enforced at the router level). Create/edit assign an AdminRole and an optional
branch_id; passwords are Argon2-hashed on write and never returned.

Prefix "/admin/users" is applied by app/api/v1/router.py.
"""
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, status
from loguru import logger
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.database import get_db
from app.core.dependencies import get_current_admin, require_roles
from app.core.security import hash_password
from app.models.admin import Admin, AdminRole
from app.models.branch import Branch
from app.models.role import Role
from app.schemas.admin_auth import CreateAdminRequest, UpdateAdminRequest

router = APIRouter(
    dependencies=[Depends(require_roles("super_admin"))],
    tags=["Admin Users"],
)


def _format_admin(admin: Admin, branch_name: str | None, role_name: str | None = None) -> dict:
    return {
        "admin_id": str(admin.admin_id),
        "email": admin.email,
        "full_name": admin.full_name,
        "role": admin.role.value if hasattr(admin.role, "value") else admin.role,
        "role_id": str(admin.role_id) if admin.role_id else None,
        "role_name": role_name,
        "branch_id": str(admin.branch_id) if admin.branch_id else None,
        "branch_name": branch_name,
        "is_active": admin.is_active,
        "last_login": admin.last_login.isoformat() if admin.last_login else None,
        "created_at": admin.created_at.isoformat() if admin.created_at else None,
    }


async def _system_role_id(db: AsyncSession, role: AdminRole) -> UUID:
    """Look up the seeded system role's role_id for one of the 5 base roles."""
    role_id = (
        await db.execute(select(Role.role_id).where(Role.system_key == role.value))
    ).scalar_one_or_none()
    if role_id is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"System role '{role.value}' is not seeded. Run migration 037.",
        )
    return role_id


async def _branch_name_map(db: AsyncSession, branch_ids: set[UUID]) -> dict[UUID, str]:
    if not branch_ids:
        return {}
    rows = (
        await db.execute(
            select(Branch.branch_id, Branch.name).where(Branch.branch_id.in_(branch_ids))
        )
    ).all()
    return dict(rows)


async def _validate_branch(db: AsyncSession, branch_id: UUID | None) -> None:
    if branch_id is None:
        return
    exists = (
        await db.execute(select(Branch.branch_id).where(Branch.branch_id == branch_id))
    ).scalar_one_or_none()
    if exists is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Assigned branch does not exist",
        )


async def _email_taken(db: AsyncSession, email: str, exclude_id: UUID | None = None) -> bool:
    stmt = select(Admin.admin_id).where(func.lower(Admin.email) == email.lower())
    if exclude_id is not None:
        stmt = stmt.where(Admin.admin_id != exclude_id)
    return (await db.execute(stmt)).scalar_one_or_none() is not None


async def _get_admin_or_404(db: AsyncSession, admin_id: UUID) -> Admin:
    result = await db.execute(select(Admin).where(Admin.admin_id == admin_id))
    admin = result.scalar_one_or_none()
    if admin is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Admin user not found",
        )
    return admin


@router.get("", response_model=dict)
async def list_admin_users(db: AsyncSession = Depends(get_db)):
    """
    List all base-role admin users, most recent first.

    Excludes delegated staff accounts (role='staff') — those are managed on
    their own surface (/admin/staff), scoped to whoever created them, not
    listed network-wide here.
    """
    admins = (
        await db.execute(
            select(Admin).where(Admin.role != AdminRole.STAFF).order_by(Admin.created_at.desc())
        )
    ).scalars().all()

    names = await _branch_name_map(db, {a.branch_id for a in admins if a.branch_id})
    return {
        "success": True,
        "data": {
            "users": [_format_admin(a, names.get(a.branch_id)) for a in admins],
        },
    }


@router.post("", response_model=dict, status_code=status.HTTP_201_CREATED)
async def create_admin_user(
    data: CreateAdminRequest,
    db: AsyncSession = Depends(get_db),
):
    """Create a base-role admin user with a role and optional branch assignment."""
    if data.role == AdminRole.STAFF:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Staff accounts are created via /admin/staff, not here.",
        )
    if await _email_taken(db, data.email):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="An admin with this email already exists",
        )
    await _validate_branch(db, data.branch_id)

    role = AdminRole(data.role.value)
    admin = Admin(
        admin_id=uuid4(),
        email=data.email.lower(),
        password_hash=hash_password(data.password),
        full_name=data.full_name.strip(),
        role=role,
        role_id=await _system_role_id(db, role),
        branch_id=data.branch_id,
        is_active=True,
    )
    db.add(admin)
    await db.commit()
    await db.refresh(admin)

    names = await _branch_name_map(db, {admin.branch_id} if admin.branch_id else set())
    logger.info(f"[admin] Admin user created: {admin.email} ({admin.role.value})")
    return {
        "success": True,
        "data": _format_admin(admin, names.get(admin.branch_id)),
        "message": "Admin user created successfully",
    }


@router.put("/{admin_id}", response_model=dict)
async def update_admin_user(
    admin_id: UUID,
    data: UpdateAdminRequest,
    db: AsyncSession = Depends(get_db),
):
    """Edit an admin user (name, email, role, branch, active status, password)."""
    admin = await _get_admin_or_404(db, admin_id)
    fields = data.model_dump(exclude_unset=True)

    if "email" in fields and fields["email"]:
        if await _email_taken(db, fields["email"], exclude_id=admin_id):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="An admin with this email already exists",
            )
        admin.email = fields["email"].lower()

    if "branch_id" in fields:
        await _validate_branch(db, fields["branch_id"])
        admin.branch_id = fields["branch_id"]

    if fields.get("password"):
        admin.password_hash = hash_password(fields["password"])

    if "role" in fields and fields["role"] is not None:
        new_role = AdminRole(fields["role"].value if hasattr(fields["role"], "value") else fields["role"])
        if new_role == AdminRole.STAFF:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Staff accounts are managed via /admin/staff, not here.",
            )
        admin.role = new_role
        admin.role_id = await _system_role_id(db, new_role)

    if "full_name" in fields and fields["full_name"]:
        admin.full_name = fields["full_name"].strip()

    if "is_active" in fields and fields["is_active"] is not None:
        admin.is_active = fields["is_active"]

    await db.commit()
    await db.refresh(admin)

    names = await _branch_name_map(db, {admin.branch_id} if admin.branch_id else set())
    logger.info(f"[admin] Admin user updated: {admin.admin_id}")
    return {
        "success": True,
        "data": _format_admin(admin, names.get(admin.branch_id)),
        "message": "Admin user updated successfully",
    }


@router.delete("/{admin_id}", response_model=dict)
async def deactivate_admin_user(
    admin_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_admin=Depends(get_current_admin),
):
    """
    Deactivate an admin user (soft delete: sets ``is_active = False``).

    A Super Admin cannot deactivate their own account (lockout guard).
    """
    if admin_id == current_admin.admin_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="You cannot deactivate your own account.",
        )

    admin = await _get_admin_or_404(db, admin_id)
    admin.is_active = False

    # Cascade: this admin's own delegated staff lose access too — an
    # orphaned-but-still-active staff account left behind after its creator
    # is deactivated would be an access-control gap, not a convenience.
    result = await db.execute(
        update(Admin).where(Admin.parent_admin_id == admin_id).values(is_active=False)
    )

    await db.commit()
    logger.info(
        f"[admin] Admin user deactivated: {admin_id} (cascaded to {result.rowcount} staff account(s))"
    )
    return {"success": True, "message": "Admin user deactivated successfully"}
