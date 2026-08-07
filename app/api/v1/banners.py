"""
Public Banner API Endpoint

The home-screen carousel the mobile app renders. Branch-scoped like the rest of
the storefront: a customer sees their own branch's banners plus the
platform-wide ones, ordered by ``sort_order``.

Works for guests as well as authenticated users — guests resolve their branch
via the ``X-Device-Id`` header, the same convention as branch resolution.

Prefix "/banners" is applied by app/api/v1/router.py.
"""
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from redis.asyncio import Redis
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.products import _get_branch_context_for_request
from app.config.database import get_db
from app.config.redis import get_redis
from app.core.dependencies import get_current_user_optional
from app.core.media import media_url
from app.models.banner import Banner

router = APIRouter(tags=["Banners"])


@router.get("", response_model=dict)
async def list_banners(
    request: Request,
    limit: int = Query(10, ge=1, le=30),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
    current_user=Depends(get_current_user_optional),
):
    """
    Active home-screen banners for the customer's branch.

    Returns the branch's own banners together with the platform-wide ones
    (``branch_id IS NULL``), in ``sort_order``. Returns
    ``400 NO_BRANCH_CONTEXT`` if no delivery location has been chosen yet.
    """
    context = await _get_branch_context_for_request(redis, request, current_user)
    branch_id = UUID(context["branch_id"])

    query = (
        select(Banner)
        .where(
            Banner.is_active.is_(True),
            or_(Banner.branch_id == branch_id, Banner.branch_id.is_(None)),
        )
        .order_by(Banner.sort_order, Banner.created_at)
        .limit(limit)
    )
    banners = (await db.execute(query)).scalars().all()

    return {
        "success": True,
        "data": {
            "banners": [
                {
                    "bannerId": str(b.banner_id),
                    "title": b.title,
                    "subtitle": b.subtitle,
                    "imageUrl": media_url(b.image_url),
                    "linkType": b.link_type,
                    "targetId": str(b.target_id) if b.target_id else None,
                }
                for b in banners
            ],
            "branch": {
                "branchId": context["branch_id"],
                "branchName": context.get("branch_name"),
            },
            "total": len(banners),
        },
    }
