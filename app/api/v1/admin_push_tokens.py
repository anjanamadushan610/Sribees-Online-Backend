"""
Staff device registration for new-order alerts (Sribees New Orders app).

POST   /admin/push-tokens               register / refresh this device
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
from app.services.staff_order_alert_service import StaffOrderAlertService

router = APIRouter()


class StaffPushTokenRequest(BaseModel):
    token: str = Field(..., min_length=1, max_length=512)
    # Required: logout stops alerts by device, so a row without one could
    # never be switched off from the phone.
    device_id: str = Field(..., min_length=1, max_length=255)
    platform: Optional[str] = None
    language: Optional[str] = Field(None, max_length=16)

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
