"""
Admin Branch Management API

Full CRUD over store branches. Creating, editing and deleting a branch is
restricted to Super Admins. Branch Managers get one narrower power on top of
that: listing (scoped to their own branch) and flipping their own branch's
``is_active`` status via ``PATCH /{branch_id}/status`` — see that route's
docstring for why. Delete is guarded: a branch that still has admins,
inventory or postal-city mappings attached cannot be removed — deactivate it
instead.

Note on fields: the branch model uses Sri-Lankan location columns
(``district`` + ``province``); the admin UI's "City" maps to ``district``.

Prefix "/admin/branches" is applied by app/api/v1/router.py.
"""
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from loguru import logger
from pydantic import BaseModel
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.database import get_db
from app.core.dependencies import get_current_admin, require_roles
from app.models.admin import Admin, AdminRole
from app.models.branch import Branch, PostalCityBranchMapping
from app.models.product import BranchInventory
from app.schemas.branch import BranchCreate, BranchUpdate, DeliveryRunsUpdate
from app.services import branch_service
from app.services.category_service import CategoryService

# Router-level: Super Admin and Branch Manager may both reach this module.
# Full CRUD (create/update/delete) is then locked back down to Super Admin
# only via an extra per-route dependency below — only listing (scoped) and
# the status-toggle route are open to Branch Managers.
router = APIRouter(
    dependencies=[Depends(require_roles("super_admin", "branch_manager"))],
    tags=["Admin Branches"],
)


def _format_branch(b: Branch, coverage_postal_cities: list[str] | None = None) -> dict:
    return {
        "branch_id": str(b.branch_id),
        "name": b.name,
        "code": b.code,
        "address": b.address,
        "district": b.district,
        "province": b.province,
        "postal_city": b.postal_city,
        "phone": b.phone,
        "manager_id": str(b.manager_id) if b.manager_id else None,
        "is_active": b.is_active,
        "delivery_runs": b.delivery_runs or [],
        "coverage_postal_cities": coverage_postal_cities or [],
        "created_at": b.created_at.isoformat() if b.created_at else None,
        "updated_at": b.updated_at.isoformat() if b.updated_at else None,
    }


async def _get_branch_or_404(db: AsyncSession, branch_id: UUID) -> Branch:
    result = await db.execute(select(Branch).where(Branch.branch_id == branch_id))
    branch = result.scalar_one_or_none()
    if branch is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Branch not found",
        )
    return branch


async def _code_taken(db: AsyncSession, code: str, exclude_id: UUID | None = None) -> bool:
    stmt = select(Branch.branch_id).where(func.lower(Branch.code) == code.lower())
    if exclude_id is not None:
        stmt = stmt.where(Branch.branch_id != exclude_id)
    return (await db.execute(stmt)).scalar_one_or_none() is not None


@router.get("", response_model=dict)
async def list_branches(
    include_inactive: bool = Query(True, description="Include inactive branches"),
    db: AsyncSession = Depends(get_db),
    admin: dict = Depends(get_current_admin),
):
    """
    List branches, with coverage.

    Super Admins see every branch (active and inactive by default). Branch
    Managers are scoped to their own branch only — same isolation rule every
    other branch-scoped admin surface applies.
    """
    stmt = select(Branch).order_by(Branch.name)
    if admin.role != AdminRole.SUPER_ADMIN:
        stmt = stmt.where(Branch.branch_id == admin.branch_id)
    if not include_inactive:
        stmt = stmt.where(Branch.is_active.is_(True))
    branches = (await db.execute(stmt)).scalars().all()
    coverage = await branch_service.get_coverage_map(db)
    return {
        "success": True,
        "data": {
            "branches": [
                _format_branch(b, coverage.get(b.branch_id, [])) for b in branches
            ]
        },
    }


@router.post(
    "",
    response_model=dict,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_roles("super_admin"))],
)
async def create_branch(
    data: BranchCreate,
    db: AsyncSession = Depends(get_db),
):
    """
    Create a new branch — Super Admin only.

    Always starts inactive (``is_active=False``, the model default): the
    branch only goes live once its Branch Manager activates it via
    ``PATCH /{branch_id}/status``.
    """
    if await _code_taken(db, data.code):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="A branch with this code already exists",
        )

    branch = Branch(
        name=data.name.strip(),
        code=data.code.strip(),
        address=data.address,
        postal_city=data.postal_city,
        district=data.district,
        province=data.province,
        phone=data.phone,
        manager_id=data.manager_id,
    )
    db.add(branch)
    # Flush (not commit) so the branch gets a PK before its coverage mappings
    # and per-category rows are written, then commit once so everything moves
    # together.
    await db.flush()
    if data.coverage_postal_cities is not None:
        await branch_service.sync_branch_coverage(
            db, branch, data.coverage_postal_cities
        )
    # Every existing category gets a hidden row for this branch — same
    # "starts dark until the branch manager turns it on" rule as the branch
    # itself and as branch_inventory.
    await CategoryService.seed_for_new_branch(db, branch.branch_id)
    await db.commit()
    await db.refresh(branch)
    coverage = await branch_service.get_branch_coverage(db, branch.branch_id)
    logger.info(f"[admin] Branch created: {branch.branch_id} - {branch.name}")
    return {
        "success": True,
        "data": _format_branch(branch, coverage),
        "message": "Branch created successfully",
    }


@router.put(
    "/{branch_id}",
    response_model=dict,
    dependencies=[Depends(require_roles("super_admin"))],
)
async def update_branch(
    branch_id: UUID,
    data: BranchUpdate,
    db: AsyncSession = Depends(get_db),
):
    """
    Update a branch (name, code, address, district, province, phone, status)
    — Super Admin only. A Branch Manager toggling their own branch's status
    uses ``PATCH /{branch_id}/status`` instead.
    """
    branch = await _get_branch_or_404(db, branch_id)
    fields = data.model_dump(exclude_unset=True)
    # coverage_postal_cities is not a Branch column — sync it separately.
    coverage_postal_cities = fields.pop("coverage_postal_cities", None)

    new_code = fields.get("code")
    if new_code and await _code_taken(db, new_code, exclude_id=branch_id):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="A branch with this code already exists",
        )

    name_changed = "name" in fields and fields["name"] and fields["name"] != branch.name
    for key, value in fields.items():
        if hasattr(branch, key):
            setattr(branch, key, value)
    await db.flush()

    # If coverage was supplied, reconcile the branch's postal city mappings.
    if coverage_postal_cities is not None:
        await branch_service.sync_branch_coverage(db, branch, coverage_postal_cities)
    elif name_changed:
        # Keep the denormalized branch_name on existing mappings in sync.
        await db.execute(
            update(PostalCityBranchMapping)
            .where(PostalCityBranchMapping.branch_id == branch.branch_id)
            .values(branch_name=branch.name)
        )

    await db.commit()
    await db.refresh(branch)
    coverage = await branch_service.get_branch_coverage(db, branch.branch_id)
    logger.info(f"[admin] Branch updated: {branch.branch_id}")
    return {
        "success": True,
        "data": _format_branch(branch, coverage),
        "message": "Branch updated successfully",
    }


@router.delete(
    "/{branch_id}",
    response_model=dict,
    dependencies=[Depends(require_roles("super_admin"))],
)
async def delete_branch(
    branch_id: UUID,
    db: AsyncSession = Depends(get_db),
):
    """
    Delete a branch. Blocked while it still has admins, inventory rows or
    postal-city mappings attached (deactivate it via PUT instead).
    """
    branch = await _get_branch_or_404(db, branch_id)

    admin_count = (
        await db.execute(
            select(func.count(Admin.admin_id)).where(Admin.branch_id == branch_id)
        )
    ).scalar() or 0
    inv_count = (
        await db.execute(
            select(func.count(BranchInventory.inventory_id)).where(
                BranchInventory.branch_id == branch_id
            )
        )
    ).scalar() or 0
    mapping_count = (
        await db.execute(
            select(func.count(PostalCityBranchMapping.mapping_id)).where(
                PostalCityBranchMapping.branch_id == branch_id
            )
        )
    ).scalar() or 0

    if admin_count or inv_count or mapping_count:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Cannot delete: branch still has {admin_count} admin(s), "
                f"{inv_count} inventory row(s) and {mapping_count} postal-city "
                f"mapping(s). Deactivate the branch instead."
            ),
        )

    await db.delete(branch)
    await db.commit()
    logger.info(f"[admin] Branch deleted: {branch_id}")
    return {"success": True, "message": "Branch deleted successfully"}


class BranchStatusUpdate(BaseModel):
    is_active: bool


@router.put("/{branch_id}/delivery-runs", response_model=dict)
async def set_branch_delivery_runs(
    branch_id: UUID,
    data: DeliveryRunsUpdate,
    db: AsyncSession = Depends(get_db),
    admin: dict = Depends(get_current_admin),
):
    """
    Replace a branch's daily delivery rounds.

    Deliberately its own route rather than a field on ``PUT /{branch_id}``
    (Super-Admin-only): the dispatch timetable is a floor-operations decision
    owned by whoever actually runs the branch, exactly like the status toggle
    above. Super Admins may set any branch's; a Branch Manager only their own.

    Stored sorted by dispatch time so every reader — the Marketing
    Dashboard's cut-off countdown included — sees the same order regardless
    of how the form submitted them.
    """
    if admin.role != AdminRole.SUPER_ADMIN and admin.branch_id != branch_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You can only change your own branch's delivery runs.",
        )

    branch = await _get_branch_or_404(db, branch_id)
    branch.delivery_runs = [
        run.model_dump()
        for run in sorted(data.delivery_runs, key=lambda r: r.dispatch_time)
    ]
    await db.commit()
    await db.refresh(branch)
    coverage = await branch_service.get_branch_coverage(db, branch.branch_id)
    logger.info(
        f"[admin] Branch {branch_id} delivery runs set to "
        f"{len(branch.delivery_runs)} round(s) by {admin.admin_id}"
    )
    return {
        "success": True,
        "data": _format_branch(branch, coverage),
        "message": "Delivery runs updated successfully",
    }


@router.patch("/{branch_id}/status", response_model=dict)
async def set_branch_status(
    branch_id: UUID,
    data: BranchStatusUpdate,
    db: AsyncSession = Depends(get_db),
    admin: dict = Depends(get_current_admin),
):
    """
    Activate or deactivate a branch.

    A Super Admin creating a branch leaves it inactive by design — it is
    invisible to customers and its inventory stays dark until someone who
    actually runs the place says it's ready. Super Admins may toggle any
    branch here; Branch Managers may only toggle their own.
    """
    if admin.role != AdminRole.SUPER_ADMIN and admin.branch_id != branch_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You can only change your own branch's status.",
        )

    branch = await _get_branch_or_404(db, branch_id)
    branch.is_active = data.is_active
    await db.commit()
    await db.refresh(branch)
    coverage = await branch_service.get_branch_coverage(db, branch.branch_id)
    logger.info(
        f"[admin] Branch {branch_id} status set to {data.is_active} by {admin.admin_id}"
    )
    return {
        "success": True,
        "data": _format_branch(branch, coverage),
        "message": f"Branch {'activated' if data.is_active else 'deactivated'} successfully",
    }
