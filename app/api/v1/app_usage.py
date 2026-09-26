"""
SRIBEESonline - App presence and usage.

  POST /app/usage/heartbeat                      -- the customer app, guest or signed in
  GET  /admin/app-usage                          -- Super Admin: online now + headline numbers
  GET  /admin/app-usage/installs                 -- Super Admin: every install, searchable
  GET  /admin/app-usage/installs/{device_id}     -- Super Admin: one install's daily history

The heartbeat is open to guests on purpose: "who has the app open" includes
people browsing before they sign in. The body can only ever describe its own
install -- it writes no customer data, and a signed-in customer is taken from
the token, never from the body.

Prefixes applied by app/api/v1/router.py.
"""
from datetime import datetime
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from loguru import logger
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.database import get_db
from app.core.dependencies import get_current_user_optional, require_roles
from app.services import app_usage_service as usage
from app.services.app_settings_service import AppSettingsService

router = APIRouter()
admin_router = APIRouter()

RequireSuperAdmin = Depends(require_roles("super_admin"))

# The mobile app's install id: "guest_<ms>_<hex>" today, kept loose enough for
# a future format but never free text.
DEVICE_ID_PATTERN = r"^[A-Za-z0-9_.:-]{8,80}$"


class HeartbeatIn(BaseModel):
    model_config = {"extra": "forbid"}

    device_id: str = Field(..., pattern=DEVICE_ID_PATTERN)
    event: Literal["open", "beat", "background"]
    foreground_seconds: int = Field(0, ge=0, le=86400)
    platform: Literal["android", "ios"]
    app_version: Optional[str] = Field(None, max_length=40, pattern=r"^[0-9A-Za-z.+_-]+$")
    build_number: Optional[str] = Field(None, max_length=20, pattern=r"^[0-9A-Za-z.+_-]+$")
    installed_at: Optional[datetime] = None


@router.post(
    "/usage/heartbeat",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=dict,
    summary="The app is open (or just went to the background)",
)
async def heartbeat(
    body: HeartbeatIn,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user_optional),
):
    await usage.record_heartbeat(
        db,
        device_id=body.device_id,
        user_id=getattr(current_user, "user_id", None),
        event=body.event,
        foreground_seconds=body.foreground_seconds,
        platform=body.platform,
        app_version=body.app_version,
        build_number=body.build_number,
        installed_at=body.installed_at,
    )
    return {"success": True}


async def _latest_version(db: AsyncSession) -> Optional[str]:
    """What the store has now, per Settings -> Mobile App. None if unknown."""
    try:
        return (await AppSettingsService.get_mobile_config(db)).get("latest_version") or None
    except Exception as exc:  # noqa: BLE001 -- a settings hiccup must not hide the page
        logger.warning(f"app-usage: latest version unavailable: {exc}")
        return None


@admin_router.get("", response_model=dict, summary="Who has the app open right now")
async def overview(db: AsyncSession = Depends(get_db), admin=RequireSuperAdmin):
    return {"success": True, "data": await usage.overview(db, await _latest_version(db))}


@admin_router.get("/installs", response_model=dict, summary="Every install of the app")
async def installs(
    search: Optional[str] = Query(None, max_length=100),
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    admin=RequireSuperAdmin,
):
    data = await usage.list_installs(
        db, latest_version=await _latest_version(db), search=search, page=page, limit=limit
    )
    return {"success": True, "data": data}


@admin_router.get(
    "/installs/{device_id}", response_model=dict, summary="One install's daily usage since download"
)
async def install_history(device_id: str, db: AsyncSession = Depends(get_db), admin=RequireSuperAdmin):
    data = await usage.install_history(db, device_id, latest_version=await _latest_version(db))
    if data is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Install not found")
    return {"success": True, "data": data}
