"""
Admin Role Management API

Lets any base-role admin define a custom-named role scoped to a subset of
their own effective permissions, for use when creating staff (see
admin_staff.py). Read endpoints are open to any authenticated admin (a staff
account needs the catalog to render its own permission picker if it's ever
extended); creation is restricted to base-role admins only — a staff account
can never define a role (require_base_role_admin enforces the one-level cap).

Prefix "/admin/roles" is applied by app/api/v1/router.py.
"""
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, status
from loguru import logger
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.database import get_db
from app.core.dependencies import get_current_admin, require_base_role_admin
from app.core.permission_catalog import EXCLUDED_RESOURCES
from app.models.admin import Admin, AdminRole
from app.models.role import Permission, Role, RolePermission
from app.schemas.roles import CreateRoleRequest, PermissionCatalogResponse, RoleListResponse, RoleOut, RoleResponse
from app.services.permission_service import PermissionService

router = APIRouter(tags=["Admin Roles"])


async def _role_out(db: AsyncSession, role: Role) -> RoleOut:
    permissions = (
        await db.execute(
            select(Permission)
            .join(RolePermission, RolePermission.permission_id == Permission.permission_id)
            .where(RolePermission.role_id == role.role_id)
        )
    ).scalars().all()
    return RoleOut(
        role_id=role.role_id,
        name=role.name,
        is_system=role.is_system,
        permissions=list(permissions),
    )


@router.get("/catalog", response_model=PermissionCatalogResponse)
async def get_permission_catalog(
    current_admin: Admin = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """The full delegatable permission catalog, plus the caller's own
    effective permission ids (frontend greys out anything not in that set)."""
    permissions = (
        await db.execute(
            select(Permission)
            .where(~Permission.resource.in_(EXCLUDED_RESOURCES))
            .order_by(Permission.resource, Permission.action)
        )
    ).scalars().all()
    my_ids = await PermissionService.get_effective_permission_ids(db, current_admin)

    return PermissionCatalogResponse(
        permissions=list(permissions),
        my_permission_ids=[p.permission_id for p in permissions if p.permission_id in my_ids],
    )


@router.get("", response_model=RoleListResponse)
async def list_roles(
    current_admin: Admin = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """System roles + roles the caller created. Super Admin additionally
    sees every custom role network-wide (oversight)."""
    query = select(Role).order_by(Role.is_system.desc(), Role.name)
    if current_admin.role != AdminRole.SUPER_ADMIN:
        # Everyone else: system roles (informational) + only their own custom roles.
        query = query.where(or_(Role.is_system.is_(True), Role.created_by_admin_id == current_admin.admin_id))

    roles = (await db.execute(query)).scalars().all()

    return RoleListResponse(roles=[await _role_out(db, r) for r in roles])


async def add_custom_role(db: AsyncSession, creator: Admin, data: CreateRoleRequest) -> Role:
    """
    Validate + stage a new custom role (and its role_permissions) on the
    session, flushed but NOT committed — lets a caller (e.g. POST
    /admin/staff) create the role and the admin that will hold it in one
    atomic transaction. Raises 400 (via PermissionService) if the requested
    permissions aren't a subset of the creator's own, or 400 (IntegrityError
    surfaced by the flush) if the creator already has a role with this name.
    """
    await PermissionService.validate_delegation(db, creator, set(data.permission_ids))

    role = Role(
        role_id=uuid4(),
        name=data.name.strip(),
        is_system=False,
        created_by_admin_id=creator.admin_id,
    )
    db.add(role)
    try:
        await db.flush()
        for permission_id in data.permission_ids:
            db.add(RolePermission(role_id=role.role_id, permission_id=permission_id))
        await db.flush()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="You already have a role with this name.",
        )
    return role


@router.post("", response_model=RoleResponse, status_code=status.HTTP_201_CREATED)
async def create_role(
    data: CreateRoleRequest,
    current_admin: Admin = Depends(require_base_role_admin),
    db: AsyncSession = Depends(get_db),
):
    """Define a new custom role scoped to a subset of the caller's own
    effective permissions. Used standalone or inline via POST /admin/staff."""
    role = await add_custom_role(db, current_admin, data)
    await db.commit()
    await db.refresh(role)
    logger.info(f"[admin] Role created: {role.name} by {current_admin.email}")
    return RoleResponse(role=await _role_out(db, role))
