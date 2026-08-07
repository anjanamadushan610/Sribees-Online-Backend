"""
SRIBEESonline - Banner Schemas

Pydantic schemas for home-screen promotional banners.
"""
from typing import Literal, Optional
from uuid import UUID

from pydantic import BaseModel, Field

# =============================================================================
# Banner Schemas
# =============================================================================

LinkType = Literal["category", "product"]


class BannerCreate(BaseModel):
    """Create a banner. Branch scoping is decided by the server, not the client."""

    title: str = Field(..., min_length=1, max_length=120)
    subtitle: Optional[str] = Field(None, max_length=200)
    image_url: Optional[str] = Field(
        None,
        max_length=500,
        description="Path returned by POST /admin/banners/upload-image",
    )
    link_type: Optional[LinkType] = Field(
        None, description="What target_id points at. Omit for an informational banner."
    )
    target_id: Optional[UUID] = Field(
        None, description="Category or product the 'Shop Now' button opens."
    )
    sort_order: int = Field(0, ge=0, description="Ascending; controls carousel order.")
    is_active: bool = True
    branch_id: Optional[UUID] = Field(
        None,
        description=(
            "Super Admin only. Omit for a platform-wide banner shown in every "
            "branch; scoped managers always get their own branch regardless."
        ),
    )


class BannerUpdate(BaseModel):
    """Update a banner — every field optional."""

    title: Optional[str] = Field(None, min_length=1, max_length=120)
    subtitle: Optional[str] = Field(None, max_length=200)
    image_url: Optional[str] = Field(None, max_length=500)
    link_type: Optional[LinkType] = None
    target_id: Optional[UUID] = None
    sort_order: Optional[int] = Field(None, ge=0)
    is_active: Optional[bool] = None
