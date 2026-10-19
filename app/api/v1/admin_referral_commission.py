"""
Admin Referral Commission API — per-product commission rates.

  GET  /settings                     programme cap and level-2 share
  PUT  /settings                     change them (Super Admin)
  PUT  /products/{product_id}        product default rate (Super Admin)
  PUT  /inventory/{inventory_id}     branch override (Super Admin, Branch Manager)
  GET  /products/{product_id}/history  who changed what, when

Role-gated rather than permission-gated, like partners and settings: this is
money, so a Branch Manager cannot delegate it to a staff account.

Prefix "/admin/referral-commission" is applied by app/api/v1/router.py.
"""
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.database import get_db
from app.core.dependencies import BranchScope, inject_branch_filter, require_roles
from app.services.referral_commission import (
    CommissionRateResolver,
    ReferralProgramConfig,
)
from app.services.referral_commission.rate_admin import (
    RateAdminService,
    RateChangeError,
    describe_rates,
)

router = APIRouter(tags=["Admin Referral Commission"])

_any_editor = require_roles("super_admin", "branch_manager")
_super_admin = require_roles("super_admin")


class RateBody(BaseModel):
    # None clears the rate (inherit the product default, or 0).
    percentage: Optional[float] = Field(None, ge=0, le=100)


class ProgrammeBody(BaseModel):
    branch_max_percent: Optional[float] = Field(None, ge=0, le=100)
    level2_share: Optional[float] = Field(None, ge=0, le=1)


def _fail(exc: RateChangeError) -> HTTPException:
    return HTTPException(
        status_code=exc.status,
        detail={
            "success": False,
            "error": {"message": str(exc), "code": exc.code, **exc.params},
        },
    )


async def _settings_payload(db: AsyncSession) -> dict:
    config = await ReferralProgramConfig.load(db)
    return {
        **config.as_dict(),
        "highest_live_percent": float(await CommissionRateResolver.highest_live_rate(db)),
    }


@router.get("/settings", response_model=dict)
async def get_programme_settings(
    db: AsyncSession = Depends(get_db),
    admin=Depends(_any_editor),
):
    """The cap a Branch Manager works under, and the upline's share."""
    return {"success": True, "data": await _settings_payload(db)}


@router.put("/settings", response_model=dict)
async def update_programme_settings(
    body: ProgrammeBody,
    db: AsyncSession = Depends(get_db),
    admin=Depends(_super_admin),
):
    try:
        await RateAdminService.update_config(
            db,
            branch_max_percent=body.branch_max_percent,
            level2_share=body.level2_share,
            admin_id=admin.admin_id,
        )
    except RateChangeError as exc:
        raise _fail(exc)
    return {"success": True, "data": await _settings_payload(db)}


@router.put("/products/{product_id}", response_model=dict)
async def set_product_default_rate(
    product_id: UUID,
    body: RateBody,
    db: AsyncSession = Depends(get_db),
    admin=Depends(_super_admin),
):
    """The rate every branch inherits unless it sets its own."""
    try:
        product = await RateAdminService.set_product_default(
            db, product_id, body.percentage, admin_id=admin.admin_id
        )
    except RateChangeError as exc:
        raise _fail(exc)
    return {
        "success": True,
        "data": {"product_id": str(product.product_id), **describe_rates(product, None)},
    }


@router.put("/inventory/{inventory_id}", response_model=dict)
async def set_branch_rate(
    inventory_id: UUID,
    body: RateBody,
    db: AsyncSession = Depends(get_db),
    admin=Depends(_any_editor),
    scope: BranchScope = Depends(inject_branch_filter),
):
    """One branch's rate for one product. Branch Managers: own branch, capped."""
    try:
        inv, product = await RateAdminService.set_branch_override(
            db,
            inventory_id,
            body.percentage,
            admin_id=admin.admin_id,
            is_super_admin=scope.is_super_admin,
            admin_branch_id=scope.branch_id,
        )
    except RateChangeError as exc:
        raise _fail(exc)
    return {
        "success": True,
        "data": {
            "inventory_id": str(inv.inventory_id),
            "product_id": str(inv.product_id),
            "branch_id": str(inv.branch_id),
            **describe_rates(product, inv),
        },
    }


@router.get("/products/{product_id}/history", response_model=dict)
async def get_rate_history(
    product_id: UUID,
    limit: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    admin=Depends(_any_editor),
    scope: BranchScope = Depends(inject_branch_filter),
):
    rows = await RateAdminService.history(db, product_id, limit)
    if not scope.is_super_admin:
        # A Branch Manager sees the product default and their own branch only.
        own = str(scope.branch_id)
        rows = [r for r in rows if r["branch_id"] in (None, own)]
    return {"success": True, "data": {"changes": rows}}
