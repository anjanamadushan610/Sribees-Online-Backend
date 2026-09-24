"""
SRIBEESonline - Service Areas (location rollout) — Super Admin only.

The three switches that decide where customers may be served
(``app/services/location_rollout_service.py``):

  GET   /admin/service-areas                                   — every province and district, with counts
  PATCH /admin/service-areas/provinces/{name}                  — unlock / lock a province
  PATCH /admin/service-areas/districts/{name}                  — unlock / lock a district
  GET   /admin/service-areas/districts/{name}/postal-areas     — its postal areas
  POST  /admin/service-areas/districts/{name}/postal-areas/activate
  POST  /admin/service-areas/postal-areas/deactivate

Prefix applied by app/api/v1/router.py.
"""
from typing import List
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.database import get_db
from app.core.dependencies import require_roles
from app.services import location_rollout_service as rollout

router = APIRouter()

RequireSuperAdmin = Depends(require_roles("super_admin"))


def _actor(admin) -> str:
    return getattr(admin, "email", None) or getattr(admin, "full_name", None) or "super_admin"


def _refused(exc: rollout.RolloutError) -> HTTPException:
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))


class UnlockBody(BaseModel):
    model_config = {"extra": "forbid"}
    is_unlocked: bool


class ActivateBody(BaseModel):
    model_config = {"extra": "forbid"}
    postal_cities: List[str] = Field(..., min_length=1, max_length=500)
    branch_id: UUID


class DeactivateBody(BaseModel):
    model_config = {"extra": "forbid"}
    postal_cities: List[str] = Field(..., min_length=1, max_length=500)


@router.get("", response_model=dict, summary="Every province and district with its rollout state")
async def overview(db: AsyncSession = Depends(get_db), admin=RequireSuperAdmin):
    return {"success": True, "data": await rollout.admin_overview(db)}


@router.patch("/provinces/{name}", response_model=dict, summary="Unlock or lock a province")
async def set_province(
    name: str, body: UnlockBody, db: AsyncSession = Depends(get_db), admin=RequireSuperAdmin
):
    try:
        await rollout.set_province_unlocked(db, name, body.is_unlocked, _actor(admin))
    except rollout.RolloutError as exc:
        raise _refused(exc)
    return {"success": True, "data": await rollout.admin_overview(db)}


@router.patch("/districts/{name}", response_model=dict, summary="Unlock or lock a district")
async def set_district(
    name: str, body: UnlockBody, db: AsyncSession = Depends(get_db), admin=RequireSuperAdmin
):
    try:
        await rollout.set_district_unlocked(db, name, body.is_unlocked, _actor(admin))
    except rollout.RolloutError as exc:
        raise _refused(exc)
    return {"success": True, "data": await rollout.admin_overview(db)}


@router.get(
    "/districts/{name}/postal-areas",
    response_model=dict,
    summary="A district's postal areas with courier state, branch and switch",
)
async def postal_areas(name: str, db: AsyncSession = Depends(get_db), admin=RequireSuperAdmin):
    try:
        return {"success": True, "data": await rollout.district_postal_areas(db, name)}
    except rollout.RolloutError as exc:
        raise _refused(exc)


@router.post(
    "/districts/{name}/postal-areas/activate",
    response_model=dict,
    summary="Map postal areas to a branch and switch them on",
)
async def activate(
    name: str, body: ActivateBody, db: AsyncSession = Depends(get_db), admin=RequireSuperAdmin
):
    try:
        result = await rollout.activate_postal_areas(db, name, body.postal_cities, body.branch_id)
    except rollout.RolloutError as exc:
        raise _refused(exc)
    return {
        "success": True,
        "data": result.to_dict(),
        "postal_areas": (await rollout.district_postal_areas(db, name))["postal_areas"],
    }


@router.post("/postal-areas/deactivate", response_model=dict, summary="Switch postal areas off")
async def deactivate(body: DeactivateBody, db: AsyncSession = Depends(get_db), admin=RequireSuperAdmin):
    count = await rollout.deactivate_postal_areas(db, body.postal_cities)
    return {"success": True, "data": {"deactivated": count}}
