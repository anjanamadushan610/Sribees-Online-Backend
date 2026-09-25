"""
SRIBEESonline - Branch Resolution API Endpoints

Routes for address-to-branch routing:
  POST /resolve                 — resolve a saved address to a branch & store context
  POST /resolve-by-location     — resolve using a province/district/postal city triplet
  GET  /context                 — retrieve the current branch context from Redis
  POST /clear                   — clear the branch context (force re-selection)
  GET  /served-postal-cities    — list all postal cities served by any branch
  GET  /public/{branch_id}      — a live branch's public name (share links)

Payloads carry ``postalCity`` and, for installed app builds, the ``postOffice``
key they read before the rename (see app/services/branch_service.py).
"""
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.database import get_db
from app.config.redis import get_redis, get_redis_optional
from app.config.settings import settings
from app.core.dependencies import get_current_user, get_current_user_optional
from app.core.exceptions import NotFoundError
from app.schemas.branch import (
    BranchResolveRequest,
    LocationResolveRequest,
)
from app.services import branch_service
from app.utils.logger import logger

router = APIRouter()
context_payload = branch_service.context_payload


# ============================================================================
# POST /resolve — resolve saved address → branch & set context
# ============================================================================

@router.post(
    "/resolve",
    response_model=dict,
    status_code=status.HTTP_200_OK,
    summary="Resolve address to serving branch",
    description=(
        "Receives a saved address ID, reads its postal city, looks up which "
        "branch serves that area, stores the branch in the user's Redis "
        "session, and returns the branch details."
    ),
)
async def resolve_branch(
    body: BranchResolveRequest,
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
    current_user=Depends(get_current_user),
):
    """
    Full address-to-branch resolution flow.

    - Validates address ownership.
    - Reads ``postal_city`` from the address.
    - Finds the ``PostalCityBranchMapping`` row.
    - Caches the result in Redis under ``session:{userId}:branch_context``.
    """
    context = await branch_service.resolve_address_to_branch(
        db=db,
        redis=redis,
        address_id=body.address_id,
        user_id=current_user.user_id,
    )

    return {
        "success": True,
        "data": context_payload(context),
        "message": f"Branch resolved: {context['branch_name']}",
    }


# ============================================================================
# POST /resolve-by-location — resolve triplet → branch & set context
# ============================================================================

@router.post(
    "/resolve-by-location",
    response_model=dict,
    status_code=status.HTTP_200_OK,
    summary="Resolve location triplet to serving branch",
    description=(
        "Receives a {province, district, postalCity} triplet from the "
        "cascading dropdown UI, resolves the branch, stores the context "
        "in Redis, and returns the branch details.  "
        "Works for authenticated users or guests (send X-Device-Id header). "
        "Returns LOCATION_NOT_SERVED if no mapping exists."
    ),
)
async def resolve_branch_by_location(
    request: Request,
    body: LocationResolveRequest,
    db: AsyncSession = Depends(get_db),
    redis: Optional[Redis] = Depends(get_redis_optional),
    current_user=Depends(get_current_user_optional),
):
    """
    Triplet-based resolution for the cascading dropdown flow.
    Authenticated: store context by user_id. Guest: require X-Device-Id header.
    Works without Redis (context not persisted if Redis unavailable).
    """
    try:
        if current_user is not None:
            context = await branch_service.resolve_location_to_branch(
                db=db,
                redis=redis,
                user_id=current_user.user_id,
                province=body.province,
                district=body.district,
                postal_city=body.postal_city,
            )
        else:
            device_id = request.headers.get("X-Device-Id") or request.headers.get("x-device-id")
            if not device_id or not device_id.strip():
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="X-Device-Id header required for guest branch resolution",
                )
            session_id = f"guest:{device_id.strip()}"
            context = await branch_service.resolve_location_to_branch_for_session(
                db=db,
                redis=redis,
                session_id=session_id,
                province=body.province,
                district=body.district,
                postal_city=body.postal_city,
            )

        return {
            "success": True,
            "data": context_payload(context),
            "message": f"Branch resolved: {context['branch_name']}",
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("resolve-by-location failed: %s", e)
        detail = str(e) if getattr(settings, "debug", False) else "An unexpected error occurred"
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={
                "success": False,
                "error": {
                    "message": detail,
                    "code": "INTERNAL_ERROR",
                    **({"type": type(e).__name__} if getattr(settings, "debug", False) else {}),
                },
            },
        )


# ============================================================================
# GET /context — retrieve current branch context
# ============================================================================

@router.get(
    "/context",
    response_model=dict,
    summary="Get current branch context",
    description=(
        "Returns the currently active branch context from the user's Redis "
        "session.  Returns 404 if no context has been set (or it expired)."
    ),
)
async def get_branch_context(
    redis: Redis = Depends(get_redis),
    current_user=Depends(get_current_user),
):
    """Retrieve branch context for the authenticated user."""
    context = await branch_service.get_branch_context(redis, current_user.user_id)

    if context is None:
        raise NotFoundError(
            resource="Branch Context",
            message="No active branch context. Please select a delivery address first.",
        )

    return {
        "success": True,
        "data": context_payload(context),
    }


# ============================================================================
# POST /clear — clear branch context
# ============================================================================

@router.post(
    "/clear",
    response_model=dict,
    summary="Clear branch context",
    description=(
        "Removes the branch context from Redis so the user must "
        "re-select a delivery address on the next app launch."
    ),
)
async def clear_branch_context(
    redis: Redis = Depends(get_redis),
    current_user=Depends(get_current_user),
):
    """Clear the authenticated user's branch context."""
    await branch_service.clear_branch_context(redis, current_user.user_id)

    return {
        "success": True,
        "message": "Branch context cleared. Please select a delivery address.",
    }


# ============================================================================
# GET /served-postal-cities — list all served postal cities
# ============================================================================

@router.get(
    "/served-postal-cities",
    response_model=dict,
    summary="List served postal cities",
    description="Returns an alphabetical list of every postal city that is actively served by a branch.",
)
@router.get("/served-post-offices", response_model=dict, include_in_schema=False)
async def list_served_postal_cities(
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Return all actively-served postal city names (sorted)."""
    postal_cities = await branch_service.list_served_postal_cities(db)

    return {
        "success": True,
        "data": {
            "postalCities": postal_cities,
            "postOffices": postal_cities,  # legacy key
            "total": len(postal_cities),
        },
    }


# ============================================================================
# GET /public/{branch_id} — the public name of a live branch
# ============================================================================

@router.get(
    "/public/{branch_id}",
    response_model=dict,
    summary="Public name of a branch",
    description=(
        "Name and district of an active branch, for anyone - a shared product "
        "link carries the sharer's branch id and the app shows where it came "
        "from. Nothing beyond what the storefront already shows."
    ),
)
async def get_public_branch(
    branch_id: UUID,
    db: AsyncSession = Depends(get_db),
):
    branch = await branch_service.get_public_branch(db, branch_id)
    if branch is None:
        raise NotFoundError(resource="Branch", message="Branch not found")
    return {
        "success": True,
        "data": {
            "branchId": str(branch.branch_id),
            "branchName": branch.name,
            "district": branch.district,
        },
    }
