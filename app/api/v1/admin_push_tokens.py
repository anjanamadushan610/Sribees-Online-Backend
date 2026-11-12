"""
Staff device registration for new-order alerts (Sribees New Orders app).

POST   /admin/push-tokens               register / refresh this device (+ can it ring)
GET    /admin/push-tokens               staff phones and whether each can ring
DELETE /admin/push-tokens/{device_id}   logout: this device stops ringing

Only admins who can see orders (``orders:read``) register; who actually
rings for an order is decided at send time (StaffOrderAlertService).
"""
from typing import Optional

from fastapi import APIRouter, Depends, Path
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.database import get_db
from app.core.dependencies import require_permission
from app.core.exceptions import AuthorizationError
from app.models.admin import AdminRole
from app.services.staff_order_alert_service import (
    CHANNEL_STATES,
    HEALTH_FIELDS,
    StaffOrderAlertService,
)

router = APIRouter()


class StaffPushTokenRequest(BaseModel):
    token: str = Field(..., min_length=1, max_length=512)
    # Required: logout stops alerts by device, so a row without one could
    # never be switched off from the phone.
    device_id: str = Field(..., min_length=1, max_length=255)
    platform: Optional[str] = None
    language: Optional[str] = Field(None, max_length=16)
    # Whether the phone can ring (app 0.8.0+); all optional, older apps omit
    # them. Display-only: an odd value is dropped, never a reason to refuse the
    # registration -- that would stop the phone ringing.
    notifications_on: Optional[bool] = None
    channel_state: Optional[str] = None
    background_restricted: Optional[bool] = None
    app_version: Optional[str] = None
    device_model: Optional[str] = None

    @field_validator("channel_state", mode="before")
    @classmethod
    def _channel(cls, v):
        v = str(v).strip().lower() if v is not None else None
        return v if v in CHANNEL_STATES else None

    @field_validator("notifications_on", "background_restricted", mode="before")
    @classmethod
    def _flag(cls, v):
        return v if isinstance(v, bool) else None

    @field_validator("app_version", "device_model", mode="before")
    @classmethod
    def _short_text(cls, v, info):
        if v is None:
            return None
        limit = 32 if info.field_name == "app_version" else 64
        return str(v).strip()[:limit] or None

    @field_validator("platform")
    @classmethod
    def _platform(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        v = v.strip().lower()
        if v not in {"android", "ios"}:
            raise ValueError("platform must be 'android' or 'ios'")
        return v


@router.post("", response_model=dict)
async def register_staff_device(
    data: StaffPushTokenRequest,
    db: AsyncSession = Depends(get_db),
    admin=Depends(require_permission("orders", "read")),
):
    row = await StaffOrderAlertService.register_device(
        db,
        admin.admin_id,
        token=data.token,
        device_id=data.device_id,
        platform=data.platform,
        language=data.language,
        health=data.model_dump(include=set(HEALTH_FIELDS)),
    )
    return {
        "success": True,
        "data": {
            "token_id": str(row.token_id),
            "device_id": row.device_id,
            "platform": row.platform,
            "language": row.language,
            "is_active": row.is_active,
        },
        "message": "Device registered for new-order alerts",
    }


@router.get("", response_model=dict)
async def list_staff_phones(
    db: AsyncSession = Depends(get_db),
    admin=Depends(require_permission("orders", "read")),
):
    """
    Which staff phones would ring for a new order, and who has none.
    Super admins see every branch; a branch manager sees their own branch.
    """
    if admin.role not in (AdminRole.SUPER_ADMIN, AdminRole.BRANCH_MANAGER):
        raise AuthorizationError("Only super admins and branch managers can see staff phones")
    if admin.role == AdminRole.BRANCH_MANAGER and admin.branch_id is None:
        raise AuthorizationError("This branch manager has no branch")
    data = await StaffOrderAlertService.staff_phones(db, admin)
    return {"success": True, "data": data}


@router.delete("/{device_id}", response_model=dict)
async def unregister_staff_device(
    device_id: str = Path(..., min_length=1, max_length=255),
    db: AsyncSession = Depends(get_db),
    admin=Depends(require_permission("orders", "read")),
):
    # Idempotent: logging out twice, or from a device that never registered,
    # is not an error.
    removed = await StaffOrderAlertService.unregister_device(db, admin.admin_id, device_id)
    return {"success": True, "data": {"deactivated": removed}}
