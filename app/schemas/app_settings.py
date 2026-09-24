"""
Pydantic Schemas for AppSettings / Splash Video Management
"""
from datetime import datetime
from typing import Optional
from uuid import UUID

from pydantic import BaseModel, Field

# ============================================================================
# Admin — Splash Video
# ============================================================================

class SplashVideoResponse(BaseModel):
    """Response showing current splash video configuration."""
    setting_id: Optional[UUID] = None
    key: str = "splash_video_url"
    video_url: Optional[str] = None
    description: Optional[str] = None
    is_active: bool = True
    updated_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


class SplashVideoUploadResponse(BaseModel):
    """Response after uploading a new splash video."""
    success: bool = True
    data: SplashVideoResponse
    message: str = "Splash video uploaded successfully"


# ============================================================================
# Admin — Platform Settings (checkout pricing + mobile app config)
# ============================================================================

class PlatformSettings(BaseModel):
    """
    Human-facing platform configuration values.

    No delivery fee: the delivery charge is SribeesExpress's quote per parcel
    and is not an administrator setting (see PricingService).
    """
    order_tax_rate_percent: float = Field(
        ..., ge=0, le=100, description="Order tax rate as a percentage (15 = 15%)"
    )
    splash_video_url: Optional[str] = Field(None, description="Splash-screen video URL")
    loyalty_spend_per_point: float = Field(
        ..., gt=0, description="Rupees of order value (excluding delivery) per loyalty point"
    )


class PlatformSettingsResponse(BaseModel):
    """GET /admin/settings response."""
    success: bool = True
    data: PlatformSettings


class PlatformSettingsUpdate(BaseModel):
    """
    PATCH /admin/settings — all fields optional (partial update).

    ``extra="forbid"``: an admin screen or script still sending
    ``flat_delivery_fee`` gets a 422 naming the field, instead of a 200 that
    silently changed nothing and let someone believe the fee was set.
    """
    model_config = {"extra": "forbid"}

    order_tax_rate_percent: Optional[float] = Field(None, ge=0, le=100)
    splash_video_url: Optional[str] = Field(None, max_length=500)
    # Applies to orders that settle after the change; points already earned
    # stay as they are.
    loyalty_spend_per_point: Optional[float] = Field(None, gt=0, le=1_000_000)


# ============================================================================
# Public — Splash Config (Flutter client)
# ============================================================================

class SplashConfigResponse(BaseModel):
    """
    Minimal payload returned to the Flutter app on startup.

    Cached aggressively in Redis since every app launch hits this endpoint.
    """
    splash_video_url: Optional[str] = Field(
        None,
        description="URL of the splash animation video. Null means no video.",
    )
    is_active: bool = Field(
        True,
        description="Whether the splash video should be shown.",
    )
