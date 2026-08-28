"""
Admin Branch Category Visibility API

Per-branch category visibility over ``branch_categories`` rows — the
category-side counterpart to Admin Branch Inventory (``admin_inventory.py``).

RBAC: super_admin, branch_manager (enforced at router level). Branch
isolation mirrors Admin Inventory: scoped admins only ever see/toggle their
own branch's rows via ``inject_branch_filter``; Super Admins must pass
``branch_id`` explicitly.

Prefix "/admin/branch-categories" is applied by app/api/v1/router.py.
"""
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from loguru import logger
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.database import get_db
from app.core.dependencies import BranchScope, inject_branch_filter, require_roles
from app.services.category_service import CategoryService

router = APIRouter(
    dependencies=[Depends(require_roles("super_admin", "branch_manager"))],
    tags=["Admin Branch Categories"],
)


class BranchCategoryStatusUpdate(BaseModel):
    is_active: bool


@router.get("", response_model=dict)
async def list_branch_categories(
    branch_id: UUID | None = Query(
        None,
        description="Super Admin only: which branch to view (ignored for scoped admins)",
    ),
    db: AsyncSession = Depends(get_db),
    scope: BranchScope = Depends(inject_branch_filter),
):
    """
    Every category with its visibility status in one branch.

    Includes categories that are globally inactive or not yet turned on here —
    a Branch Manager needs to see everything to decide what to activate, not
    just what customers already see.
    """
    effective_branch = scope.resolve(branch_id)
    if effective_branch is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Specify ?branch_id= to view category visibility.",
        )

    rows = await CategoryService.list_for_branch(db, effective_branch)
    return {
        "success": True,
        "data": {
            "branch_id": str(effective_branch),
            "categories": [
                {
                    "category_id": str(cat.category_id),
                    "name": cat.name,
                    "slug": cat.slug,
                    "parent_category_id": (
                        str(cat.parent_category_id) if cat.parent_category_id else None
                    ),
                    "globally_active": cat.is_active,
                    "is_active": is_active,
                }
                for cat, is_active in rows
            ],
        },
    }


@router.patch("/{category_id}", response_model=dict)
async def set_branch_category_status(
    category_id: UUID,
    data: BranchCategoryStatusUpdate,
    branch_id: UUID | None = Query(
        None,
        description="Super Admin only: which branch to update (ignored for scoped admins)",
    ),
    db: AsyncSession = Depends(get_db),
    scope: BranchScope = Depends(inject_branch_filter),
):
    """
    Activate/deactivate a category for one branch.

    A brand new category starts hidden in every branch until that branch's
    own manager turns it on here — same rule as a new branch and a newly
    stocked product.
    """
    effective_branch = scope.resolve(branch_id)
    if effective_branch is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="branch_id is required when updating as Super Admin.",
        )

    category = await CategoryService.get_by_id(db, category_id)
    if category is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Category not found",
        )

    row = await CategoryService.set_branch_status(
        db, category_id, effective_branch, data.is_active
    )
    logger.info(
        f"[admin] Category {category_id} {'activated' if data.is_active else 'deactivated'} "
        f"for branch {effective_branch}"
    )
    return {
        "success": True,
        "data": {
            "category_id": str(row.category_id),
            "branch_id": str(row.branch_id),
            "is_active": row.is_active,
        },
        "message": f"Category {'activated' if data.is_active else 'deactivated'} for this branch",
    }
