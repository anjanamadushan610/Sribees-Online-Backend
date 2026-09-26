"""
SRIBEESonline - GET /app/sync-state: has anything the app shows changed?

A few bytes the app asks for on open, when a silent push arrives, and on its
30-minute fallback timer. It compares each version with the last one it saw
and re-reads only the groups that moved (app/services/live_sync.py).

Guests are welcome: the catalog version includes their branch when they have
chosen a delivery location (X-Device-Id), and "user" is null for them.

Prefix "/app" applied by app/api/v1/router.py.
"""
from fastapi import APIRouter, Depends, Request
from redis.asyncio import Redis

from app.api.v1.products import _get_branch_context_for_request
from app.config.redis import get_redis
from app.core.dependencies import get_current_user_optional
from app.services import live_sync

router = APIRouter()


@router.get("/sync-state", response_model=dict, summary="Versions of what the app shows")
async def sync_state(
    request: Request,
    redis: Redis = Depends(get_redis),
    current_user=Depends(get_current_user_optional),
):
    branch_id = None
    try:
        context = await _get_branch_context_for_request(redis, request, current_user)
        branch_id = context.get("branch_id")
    except Exception:  # noqa: BLE001 -- no delivery location yet: product-level only
        branch_id = None

    user_id = str(current_user.user_id) if current_user is not None else None
    return {
        "success": True,
        "data": {
            **await live_sync.versions(branch_id, user_id),
            "push_topic": live_sync.PUSH_TOPIC,
        },
    }
