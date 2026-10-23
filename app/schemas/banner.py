"""
SRIBEESonline - Banner Schemas

Pydantic schemas for home-screen promotional banners.
"""
from datetime import datetime
from typing import Literal, Optional
from uuid import UUID

from pydantic import BaseModel, Field, model_validator

# =============================================================================
# Banner Schemas
# =============================================================================

# 'category'/'product' navigate to one row and need a target_id;
# 'quick_sale' is a fixed screen and deliberately carries none, so a
# banner switched to it cannot keep pointing at a stale product.
LinkType = Literal["category", "product", "quick_sale"]


def _check_window(starts_at, ends_at) -> None:
    if starts_at is not None and ends_at is not None and ends_at <= starts_at:
        raise ValueError("ends_at must be after starts_at")


def _check_target(link_type, target_id) -> None:
    """
    A destination must be reachable.

    A 'category'/'product' banner with no target_id renders a Shop Now button
    that does nothing on the customer's phone — the worst kind of bug, because
    it looks fine in the admin table. And 'quick_sale' must NOT carry a target,
    or editing a product banner into a Quick Sale one leaves a stale id behind
    that the next edit would silently resurrect.
    """
    if link_type in ("category", "product") and target_id is None:
        raise ValueError(f"A '{link_type}' banner needs a target_id to open")
    if link_type == "quick_sale" and target_id is not None:
        raise ValueError("A 'quick_sale' banner opens a fixed screen and takes no target_id")


def _validate_banner_fields(model):
    _check_window(model.starts_at, model.ends_at)
    _check_target(model.link_type, model.target_id)
    return model


class BannerCreate(BaseModel):
    """Create a banner. Branch scoping is decided by the server, not the client."""

    title: Optional[str] = Field(None, max_length=120)
    subtitle: Optional[str] = Field(None, max_length=200)
    description: Optional[str] = Field(None, max_length=300)
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
    # Scheduling. Both omitted = always active. The STOREFRONT filters on
    # these, so a campaign banner leaves customers' phones the moment it
    # expires instead of waiting for someone to remember to switch it off.
    starts_at: Optional[datetime] = Field(
        None, description="Banner appears from this moment. Omit for immediately."
    )
    ends_at: Optional[datetime] = Field(
        None, description="Banner hides itself after this moment. Omit for never."
    )
    branch_id: Optional[UUID] = Field(
        None,
        description=(
            "Super Admin only. Omit for a platform-wide banner shown in every "
            "branch; scoped managers always get their own branch regardless."
        ),
    )

    @model_validator(mode="after")
    def _check(self) -> "BannerCreate":
        if not (self.title and self.title.strip()) and not (self.image_url and self.image_url.strip()):
            raise ValueError("A banner must have either a title or an image")
        return _validate_banner_fields(self)


class BannerUpdate(BaseModel):
    """Update a banner — every field optional."""

    title: Optional[str] = Field(None, max_length=120)
    subtitle: Optional[str] = Field(None, max_length=200)
    description: Optional[str] = Field(None, max_length=300)
    image_url: Optional[str] = Field(None, max_length=500)
    link_type: Optional[LinkType] = None
    target_id: Optional[UUID] = None
    sort_order: Optional[int] = Field(None, ge=0)
    is_active: Optional[bool] = None
    starts_at: Optional[datetime] = None
    ends_at: Optional[datetime] = None

    @model_validator(mode="after")
    def _check(self) -> "BannerUpdate":
        # Only when both sides are present: a partial update that moves one end
        # is validated against the stored value in the router.
        if self.starts_at is not None and self.ends_at is not None:
            _check_window(self.starts_at, self.ends_at)
        if self.link_type is not None:
            _check_target(self.link_type, self.target_id)
        return self
