"""
SRIBEESonline - Location Discovery API Endpoints

Public / customer-facing endpoints for cascading address selection:
  GET /provinces           — all provinces with coverage metadata
  GET /districts           — districts within a province
  GET /postal-cities       — postal cities within a district

``GET /post-offices`` answers too, identically and hidden from the schema: it
is the path app builds from before the postal-city rename call.
"""
from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.database import get_db
from app.core.dependencies import get_current_user_optional
from app.services import branch_service

router = APIRouter()


# ============================================================================
# GET /provinces
# ============================================================================

@router.get(
    "/provinces",
    response_model=dict,
    summary="List all served provinces",
    description=(
        "Returns every province a customer can be served in (unlocked, with at "
        "least one active postal area). With include_upcoming, every province, "
        "each marked available, so locked ones can be shown as Coming soon. "
        "Each carries coverage metadata (district count, post-office "
        "count, branch count, and a plain-English coverage summary). "
        "Works for both authenticated and guest users."
    ),
)
async def list_provinces(
    include_upcoming: bool = Query(
        False,
        description=(
            "Also list locked ones, marked available=false, so the app can show "
            "them as Coming soon."
        ),
    ),
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user_optional),
):
    provinces = await branch_service.list_provinces(db, include_upcoming=include_upcoming)

    return {
        "success": True,
        "data": provinces,
        "total": len(provinces),
    }


# ============================================================================
# GET /districts?province=
# ============================================================================

@router.get(
    "/districts",
    response_model=dict,
    summary="List districts for a province",
    description=(
        "Given a province name, returns the districts within it that have "
        "active branch mappings.  Each district includes the serving branch "
        "names and an AI-readable coverage summary."
    ),
)
async def list_districts(
    province: str = Query(
        ...,
        min_length=1,
        description="Province name (title-case normalised automatically)",
    ),
    include_upcoming: bool = Query(
        False,
        description=(
            "Also list locked ones, marked available=false, so the app can show "
            "them as Coming soon."
        ),
    ),
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user_optional),
):
    districts = await branch_service.list_districts(db, province, include_upcoming=include_upcoming)

    return {
        "success": True,
        "data": districts,
        "total": len(districts),
    }


# ============================================================================
# GET /postal-cities?district=
# ============================================================================

@router.get(
    "/postal-cities",
    response_model=dict,
    summary="List postal cities for a district",
    description=(
        "Given a district name, returns every active postal city within it "
        "together with the mapped branch ID and name — ready for the final "
        "step of the cascading dropdown."
    ),
)
@router.get("/post-offices", response_model=dict, include_in_schema=False)
async def list_postal_cities(
    district: str = Query(
        ...,
        min_length=1,
        description="District name (title-case normalised automatically)",
    ),
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user_optional),
):
    postal_cities = await branch_service.list_postal_cities_for_district(db, district)

    return {
        "success": True,
        "data": postal_cities,
        "total": len(postal_cities),
    }
