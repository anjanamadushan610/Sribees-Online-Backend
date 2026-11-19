"""
SRIBEESonline - customer activity log.

  POST /app/activity/batch   -- the customer app and the web store, guest or signed in
  GET  /admin/activity       -- Super Admin: one timeline, by customer or by install

Open to guests like the heartbeat: what a shopper looked at before signing up
is the point (owner, 2026-11-19). The body only describes its own install;
the customer is taken from the token, never from the body.

Prefixes applied by app/api/v1/router.py.
"""
from datetime import datetime
from typing import List, Literal, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.app_usage import DEVICE_ID_PATTERN
from app.config.database import get_db
from app.core.dependencies import get_current_user_optional, require_roles
from app.core.rate_limiter import rate_limit
from app.services import activity_service as activity

router = APIRouter()
admin_router = APIRouter()

RequireSuperAdmin = Depends(require_roles("super_admin"))

_NAME = r"^[a-z][a-z0-9_]{0,39}$"


class EventIn(BaseModel):
    model_config = {"extra": "forbid"}

    event: str = Field(..., pattern=_NAME, max_length=30)
    occurred_at: Optional[datetime] = None
    screen: Optional[str] = Field(None, pattern=_NAME, max_length=40)
    product_id: Optional[UUID] = None
    category_id: Optional[UUID] = None
    props: Optional[dict] = None


class BatchIn(BaseModel):
    model_config = {"extra": "forbid"}

    device_id: str = Field(..., pattern=DEVICE_ID_PATTERN)
    platform: Literal["android", "ios", "web"]
    app_version: Optional[str] = Field(None, max_length=40, pattern=r"^[0-9A-Za-z.+_-]+$")
    session_id: Optional[str] = Field(None, max_length=40, pattern=r"^[A-Za-z0-9_.:-]+$")
    events: List[EventIn] = Field(..., min_length=1, max_length=activity.MAX_EVENTS_PER_BATCH)


@router.post(
    "/activity/batch",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=dict,
    summary="What the shopper did since the last batch",
)
# Generous: many phones share one mobile-carrier address, and a phone sends
# about two batches a minute while in use.
@rate_limit(requests_per_minute=120)
async def batch(
    body: BatchIn,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user_optional),
):
    try:
        written = await activity.record_batch(
            db,
            device_id=body.device_id,
            user_id=getattr(current_user, "user_id", None),
            platform=body.platform,
            app_version=body.app_version,
            session_id=body.session_id,
            events=[e.model_dump() for e in body.events],
        )
    except activity.ActivityRejected as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    return {"success": True, "data": {"accepted": written}}


@admin_router.get("", response_model=dict, summary="One customer's or one install's activity, newest first")
async def read_timeline(
    user_id: Optional[UUID] = Query(None),
    device_id: Optional[str] = Query(None, pattern=DEVICE_ID_PATTERN),
    before_id: Optional[int] = Query(None, ge=1),
    limit: int = Query(200, ge=1, le=activity.TIMELINE_PAGE_MAX),
    db: AsyncSession = Depends(get_db),
    admin=RequireSuperAdmin,
):
    if (user_id is None) == (device_id is None):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Give user_id or device_id")
    data = await activity.timeline(db, user_id=user_id, device_id=device_id, before_id=before_id, limit=limit)
    return {"success": True, "data": data}
