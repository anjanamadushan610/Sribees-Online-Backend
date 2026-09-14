"""
SRIBEESonline - Session Location API (Module 2.3)

The platform is hyper-local: a customer's active delivery location dictates the
branch whose catalog, prices, stock and discounts they see. This router lets the
client set that location from a saved address; the resolved branch is stored in
the customer's Redis session so every subsequent GET /products scopes to it.

  POST /session/set-location  — resolve an address → branch, save to session
  GET  /session/location      — read the current session location/branch

Prefix "/session" is applied by app/api/v1/router.py.
"""
from fastapi import APIRouter, Depends, status
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.database import get_db
from app.config.redis import get_redis
from app.core.dependencies import get_current_user
from app.core.exceptions import NotFoundError
from app.schemas.branch import BranchResolveRequest
from app.services import branch_service

router = APIRouter(tags=["Session"])
_context_payload = branch_service.context_payload


@router.post(
    "/set-location",
    response_model=dict,
    status_code=status.HTTP_200_OK,
    summary="Set the active delivery location from a saved address",
    description=(
        "Resolves the given address's postal city to its serving branch and "
        "stores the branch in the customer's Redis session. All subsequent "
        "product/pricing calls are then scoped to this branch."
    ),
)
async def set_location(
    body: BranchResolveRequest,
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
    current_user=Depends(get_current_user),
):
    context = await branch_service.resolve_address_to_branch(
        db=db,
        redis=redis,
        address_id=body.address_id,
        user_id=current_user.user_id,
    )
    return {
        "success": True,
        "data": _context_payload(context),
        "message": f"Delivering from {context['branch_name']}",
    }


@router.get(
    "/location",
    response_model=dict,
    summary="Get the current session delivery location",
    description="Returns the active branch context, or 404 if none is set.",
)
async def get_location(
    redis: Redis = Depends(get_redis),
    current_user=Depends(get_current_user),
):
    context = await branch_service.get_branch_context(redis, current_user.user_id)
    if context is None:
        raise NotFoundError(
            resource="Session Location",
            message="No delivery location set. Please select a delivery address first.",
        )
    return {"success": True, "data": _context_payload(context)}
