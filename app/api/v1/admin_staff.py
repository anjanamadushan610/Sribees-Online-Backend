"""
Admin Staff Management API

Lets any base-role admin create/manage staff accounts beneath themselves,
scoped to a subset of their own permissions (see admin_roles.py for how the
role/permission set itself is defined). Deliberately separate from
admin_users.py, which stays exactly as-is: Super-Admin-only CRUD over the 5
base roles — an unrelated concern.

Every route requires require_base_role_admin — a staff account can never
reach this router at all, which is what enforces the "one hierarchy level
only" rule (staff cannot create or manage further staff).

Prefix "/admin/staff" is applied by app/api/v1/router.py.
"""
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, status
from loguru import logger
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.admin_roles import _role_out, add_custom_role
from app.api.v1.admin_users import _branch_name_map, _email_taken, _validate_branch
from app.config.database import get_db
from app.core.dependencies import require_base_role_admin
from app.core.security import hash_password
from app.models.admin import Admin, AdminRole
from app.models.role import Role, RolePermission
from app.schemas.roles import (
    CreateStaffRequest,
    StaffListResponse,
    StaffOut,
    StaffResponse,
    UpdateStaffRequest,
)
from app.services.audit_service import AuditAction, AuditService
from app.services.permission_service import PermissionService

router = APIRouter(dependencies=[Depends(require_base_role_admin)], tags=["Admin Staff"])


async def _staff_out(db: AsyncSession, admin: Admin, branch_name: str | None) -> StaffOut:
    role = (await db.execute(select(Role).where(Role.role_id == admin.role_id))).scalar_one()
    return StaffOut(
        admin_id=admin.admin_id,
        email=admin.email,
        full_name=admin.full_name,
        role=await _role_out(db, role),
        branch_id=admin.branch_id,
        branch_name=branch_name,
        parent_admin_id=admin.parent_admin_id,
        is_active=admin.is_active,
        created_at=admin.created_at,
    )


async def _get_own_staff_or_404(db: AsyncSession, current_admin: Admin, admin_id: UUID) -> Admin:
    """404s if the id doesn't exist or isn't a staff row; 403s if caller is not Super Admin
    and staff's assigned branch doesn't match caller's branch."""
    target = (
        await db.execute(select(Admin).where(Admin.admin_id == admin_id, Admin.role == AdminRole.STAFF))
    ).scalar_one_or_none()
    if target is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Staff account not found")
    if current_admin.role != AdminRole.SUPER_ADMIN:
        if current_admin.branch_id is None or target.branch_id != current_admin.branch_id:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not a staff account in your branch")
        # Branch-wide management (2f3f022) must not be a way up: whoever can
        # edit a staff account can reset its password and sign in as it, so a
        # Marketing Manager editing staff the Branch Manager created would
        # borrow permissions they were never given. Only staff whose grants
        # the caller already holds are theirs to manage.
        if target.parent_admin_id != current_admin.admin_id:
            theirs = await PermissionService.get_effective_permission_ids(db, target)
            mine = await PermissionService.get_effective_permission_ids(db, current_admin)
            if not theirs <= mine:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="This staff account has permissions you do not hold.",
                )
    return target


@router.get("", response_model=StaffListResponse)
async def list_staff(
    current_admin: Admin = Depends(require_base_role_admin),
    db: AsyncSession = Depends(get_db),
):
    """Super Admin sees every staff account network-wide; Branch Managers see all staff
    assigned to their own branch."""
    query = select(Admin).where(Admin.role == AdminRole.STAFF).order_by(Admin.created_at.desc())
    if current_admin.role != AdminRole.SUPER_ADMIN:
        if current_admin.branch_id is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Your account is not assigned to a branch.",
            )
        query = query.where(Admin.branch_id == current_admin.branch_id)

    staff = (await db.execute(query)).scalars().all()
    names = await _branch_name_map(db, {a.branch_id for a in staff if a.branch_id})

    return StaffListResponse(
        staff=[await _staff_out(db, a, names.get(a.branch_id)) for a in staff],
        total=len(staff),
    )


@router.post("", response_model=StaffResponse, status_code=status.HTTP_201_CREATED)
async def create_staff(
    data: CreateStaffRequest,
    current_admin: Admin = Depends(require_base_role_admin),
    db: AsyncSession = Depends(get_db),
):
    """Create a staff account under the caller, holding either a role they
    already defined (role_id) or one defined inline (new_role)."""
    if await _email_taken(db, data.email):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="An admin with this email already exists",
        )

    # Branch isolation: only a creator who has no home branch of their own
    # (Super Admin, and a branchless Customer Support lead) may name the
    # branch. A scoped base-role admin — Branch / Marketing / Inventory
    # Manager — always creates inside their OWN branch, so a `branchId` in
    # the request body is ignored rather than trusted: otherwise Colombo's
    # Marketing Manager could mint an account that reads Kandy's data.
    #
    # A branch-unscoped staff account would also be silently unable to use any
    # branch-scoped route later, since 'staff' is not in UNSCOPED_ROLES —
    # require a resolvable branch now.
    branch_id = current_admin.branch_id or data.branch_id
    if branch_id is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="A branch must be assigned for this staff account.",
        )
    await _validate_branch(db, branch_id)

    if data.new_role is not None:
        role = await add_custom_role(db, current_admin, data.new_role)
    else:
        role = (
            await db.execute(select(Role).where(Role.role_id == data.role_id))
        ).scalar_one_or_none()
        if role is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Role not found.")
        if role.is_system:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Staff accounts cannot hold a system role.",
            )
        if role.created_by_admin_id != current_admin.admin_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You can only assign roles you created yourself.",
            )

    admin = Admin(
        admin_id=uuid4(),
        email=data.email.lower(),
        password_hash=hash_password(data.password),
        full_name=data.full_name.strip(),
        role=AdminRole.STAFF,
        role_id=role.role_id,
        parent_admin_id=current_admin.admin_id,
        branch_id=branch_id,
        is_active=True,
    )
    db.add(admin)
    await db.commit()
    await db.refresh(admin)

    await AuditService.log_admin_action(
        db=db,
        action=AuditAction.ADMIN_CREATE,
        admin_id=current_admin.admin_id,
        admin_email=current_admin.email,
        target_type="admin",
        target_id=admin.admin_id,
        description=f"Created staff '{admin.email}' with role '{role.name}'",
        changes={"role_id": str(role.role_id), "role_name": role.name},
    )

    names = await _branch_name_map(db, {branch_id})
    logger.info(f"[admin] Staff created: {admin.email} by {current_admin.email}")
    return StaffResponse(
        staff=await _staff_out(db, admin, names.get(branch_id)),
        message="Staff account created successfully",
    )


@router.put("/{admin_id}", response_model=StaffResponse)
async def update_staff(
    admin_id: UUID,
    data: UpdateStaffRequest,
    current_admin: Admin = Depends(require_base_role_admin),
    db: AsyncSession = Depends(get_db),
):
    """
    Edit a staff account. `permission_ids`, if present, replaces the
    permission set on the staff's underlying ROLE — since a role can be
    reused across several staff, this affects everyone holding that role,
    which mirrors "editing a role definition" rather than "editing one
    person's grant". Re-validated against the caller's CURRENT effective
    permissions (which may have shrunk since the role was first created).
    """
    target = await _get_own_staff_or_404(db, current_admin, admin_id)
    fields = data.model_dump(exclude_unset=True, exclude={"permission_ids"})

    if fields.get("branch_id") is not None:
        # Same isolation rule as create: a scoped admin may not transfer one of
        # their staff into another branch (which would hand that account the
        # other branch's data while still being "their" staff member).
        if current_admin.branch_id is not None and fields["branch_id"] != current_admin.branch_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You can only assign staff to your own branch.",
            )
        await _validate_branch(db, fields["branch_id"])
        target.branch_id = fields["branch_id"]
    if fields.get("full_name"):
        target.full_name = fields["full_name"].strip()
    if fields.get("password"):
        target.password_hash = hash_password(fields["password"])
    if "is_active" in fields and fields["is_active"] is not None:
        target.is_active = fields["is_active"]

    if data.permission_ids is not None:
        role = (await db.execute(select(Role).where(Role.role_id == target.role_id))).scalar_one()
        await PermissionService.validate_delegation(db, current_admin, set(data.permission_ids))
        await db.execute(delete(RolePermission).where(RolePermission.role_id == role.role_id))
        for permission_id in data.permission_ids:
            db.add(RolePermission(role_id=role.role_id, permission_id=permission_id))

    await db.commit()
    await db.refresh(target)

    names = await _branch_name_map(db, {target.branch_id} if target.branch_id else set())
    logger.info(f"[admin] Staff updated: {target.email} by {current_admin.email}")
    return StaffResponse(
        staff=await _staff_out(db, target, names.get(target.branch_id)),
        message="Staff account updated successfully",
    )


@router.delete("/{admin_id}", response_model=dict)
async def deactivate_staff(
    admin_id: UUID,
    current_admin: Admin = Depends(require_base_role_admin),
    db: AsyncSession = Depends(get_db),
):
    """Soft-delete a staff account. A staff account can never call this
    itself (require_base_role_admin blocks it), so there is no self-lockout
    case to guard against here."""
    target = await _get_own_staff_or_404(db, current_admin, admin_id)
    target.is_active = False
    await db.commit()
    logger.info(f"[admin] Staff deactivated: {admin_id} by {current_admin.email}")
    return {"success": True, "message": "Staff account deactivated successfully"}
