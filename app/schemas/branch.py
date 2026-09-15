"""
SRIBEESonline - Branch & Postal City Mapping Schemas

Pydantic schemas for branch routing request/response validation.

**Wire compatibility.** The place an address is written in is a *postal city*
(see app/models/branch.py). Requests still accept the old ``post_office`` /
``postOffice`` spellings through ``AliasChoices``: mobile builds already on
customers' phones send them, and refusing those would break address saving
and branch selection for everyone who has not updated. Responses carry the new
names, alongside the old ones where an installed client reads them — see the
routes that build those payloads.
"""
from datetime import datetime
from typing import Optional
from uuid import UUID

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

#: Every spelling a client may send for a postal city, newest first.
POSTAL_CITY_ALIASES = AliasChoices("postal_city", "postalCity", "post_office", "postOffice")

# =============================================================================
# Branch Schemas
# =============================================================================

class BranchBase(BaseModel):
    """Base branch fields."""
    model_config = ConfigDict(populate_by_name=True)

    name: str = Field(..., min_length=1, max_length=255)
    code: str = Field(..., min_length=2, max_length=50)
    address: Optional[str] = None
    postal_city: Optional[str] = Field(None, max_length=100, validation_alias=POSTAL_CITY_ALIASES)
    district: Optional[str] = Field(None, max_length=100)
    province: str = Field(..., max_length=100)
    phone: Optional[str] = Field(None, max_length=20)


class BranchCreate(BranchBase):
    """Create branch request."""
    manager_id: Optional[UUID] = None
    coverage_postal_cities: Optional[list[str]] = Field(
        None,
        validation_alias=AliasChoices("coverage_postal_cities", "coverage_post_offices"),
        description=(
            "Postal city names this branch serves. Synced to "
            "PostalCityBranchMapping in the same transaction."
        ),
    )


class BranchUpdate(BaseModel):
    """Update branch request — all fields optional."""
    model_config = ConfigDict(populate_by_name=True)

    name: Optional[str] = Field(None, min_length=1, max_length=255)
    code: Optional[str] = Field(None, min_length=2, max_length=50)
    address: Optional[str] = None
    postal_city: Optional[str] = Field(None, max_length=100, validation_alias=POSTAL_CITY_ALIASES)
    district: Optional[str] = Field(None, max_length=100)
    province: Optional[str] = Field(None, max_length=100)
    phone: Optional[str] = Field(None, max_length=20)
    manager_id: Optional[UUID] = None
    is_active: Optional[bool] = None
    coverage_postal_cities: Optional[list[str]] = Field(
        None,
        validation_alias=AliasChoices("coverage_postal_cities", "coverage_post_offices"),
        description=(
            "Full replacement set of postal cities this branch serves. When "
            "provided, the branch's PostalCityBranchMapping rows are synced to "
            "exactly this list. Omit to leave coverage unchanged."
        ),
    )


class DeliveryRun(BaseModel):
    """
    One daily dispatch round for a branch.

    ``dispatch_time`` is when the van leaves, in 24-hour ``HH:MM`` Asia/Colombo
    (Sri Lanka has one timezone and no DST, so a wall-clock string is exact).
    ``cutoff_minutes`` is how long before that ordering closes, which is the
    number the Marketing Dashboard counts down to — a manager can still act on
    a run whose cut-off has not passed, and can do nothing about one that has.
    """

    label: str = Field(..., min_length=1, max_length=40)
    dispatch_time: str = Field(..., pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    cutoff_minutes: int = Field(0, ge=0, le=24 * 60)


class DeliveryRunsUpdate(BaseModel):
    """
    Full replacement set of a branch's daily delivery runs.

    An empty list is allowed and means "this branch has not set its rounds up",
    which the dashboard renders as a prompt rather than inventing a schedule.
    """

    delivery_runs: list[DeliveryRun] = Field(..., max_length=12)

    @model_validator(mode="after")
    def _no_duplicate_times(self) -> "DeliveryRunsUpdate":
        times = [r.dispatch_time for r in self.delivery_runs]
        if len(times) != len(set(times)):
            raise ValueError("Two delivery runs cannot share the same dispatch time")
        return self


class BranchResponse(BaseModel):
    """Branch response."""
    model_config = ConfigDict(from_attributes=True, populate_by_name=True)

    branch_id: UUID = Field(..., alias="branchId")
    name: str
    code: str
    address: Optional[str] = None
    postal_city: Optional[str] = Field(None, alias="postalCity")
    district: Optional[str] = None
    province: str
    phone: Optional[str] = None
    manager_id: Optional[UUID] = Field(None, alias="managerId")
    is_active: bool = Field(..., alias="isActive")
    created_at: datetime = Field(..., alias="createdAt")
    updated_at: datetime = Field(..., alias="updatedAt")


# =============================================================================
# Postal City → Branch Mapping Schemas
# =============================================================================

class PostalCityBranchMappingBase(BaseModel):
    """Base mapping fields."""
    model_config = ConfigDict(populate_by_name=True)

    postal_city: str = Field(
        ...,
        min_length=1,
        max_length=100,
        validation_alias=POSTAL_CITY_ALIASES,
        description="Postal city name (must match an address's postal city)",
    )
    branch_id: UUID = Field(..., description="Serving branch UUID")
    branch_name: str = Field(
        ...,
        min_length=1,
        max_length=255,
        description="Denormalized branch name for fast reads",
    )
    district: Optional[str] = Field(None, max_length=100)
    province: Optional[str] = Field(None, max_length=100)

    @field_validator("postal_city")
    @classmethod
    def normalize_postal_city(cls, v: str) -> str:
        """Normalize postal city name: strip whitespace, title case."""
        return v.strip().title()


class PostalCityBranchMappingCreate(PostalCityBranchMappingBase):
    """Create mapping request."""
    is_active: bool = True


class PostalCityBranchMappingUpdate(BaseModel):
    """Update mapping request — all fields optional."""
    model_config = ConfigDict(populate_by_name=True)

    postal_city: Optional[str] = Field(
        None, min_length=1, max_length=100, validation_alias=POSTAL_CITY_ALIASES
    )
    branch_id: Optional[UUID] = None
    branch_name: Optional[str] = Field(None, min_length=1, max_length=255)
    district: Optional[str] = Field(None, max_length=100)
    province: Optional[str] = Field(None, max_length=100)
    is_active: Optional[bool] = None

    @field_validator("postal_city")
    @classmethod
    def normalize_postal_city(cls, v: Optional[str]) -> Optional[str]:
        if v is not None:
            return v.strip().title()
        return v


class PostalCityBranchMappingResponse(BaseModel):
    """Mapping response."""
    model_config = ConfigDict(from_attributes=True, populate_by_name=True)

    mapping_id: UUID = Field(..., alias="mappingId")
    postal_city: str = Field(..., alias="postalCity")
    branch_id: UUID = Field(..., alias="branchId")
    branch_name: str = Field(..., alias="branchName")
    district: Optional[str] = None
    province: Optional[str] = None
    is_active: bool = Field(..., alias="isActive")
    created_at: datetime = Field(..., alias="createdAt")
    updated_at: datetime = Field(..., alias="updatedAt")


class PostalCityBranchMappingListResponse(BaseModel):
    """List of mappings response."""
    success: bool = True
    mappings: list[PostalCityBranchMappingResponse]
    total: int


# =============================================================================
# Branch Resolution Schemas (Customer-facing)
# =============================================================================

class BranchResolveRequest(BaseModel):
    """Request to resolve a delivery address to a branch."""
    model_config = ConfigDict(populate_by_name=True)

    address_id: UUID = Field(..., alias="addressId", description="User's saved address UUID")


class BranchResolveResponse(BaseModel):
    """Response after resolving an address to a branch."""
    model_config = ConfigDict(populate_by_name=True)

    success: bool = True
    branch_id: UUID = Field(..., alias="branchId")
    branch_name: str = Field(..., alias="branchName")
    postal_city: str = Field(..., alias="postalCity")
    resolved_at: datetime = Field(..., alias="resolvedAt")


class BranchContextResponse(BaseModel):
    """Current branch context from session."""
    model_config = ConfigDict(populate_by_name=True)

    success: bool = True
    branch_id: UUID = Field(..., alias="branchId")
    branch_name: str = Field(..., alias="branchName")
    postal_city: str = Field(..., alias="postalCity")
    resolved_at: datetime = Field(..., alias="resolvedAt")


class BranchContextClearedResponse(BaseModel):
    """Response when branch context is cleared."""
    success: bool = True
    message: str = "Branch context cleared. Please select a delivery address."


class ServedPostalCitiesResponse(BaseModel):
    """List of all served postal cities."""
    success: bool = True
    postal_cities: list[str]
    total: int


# =============================================================================
# Location Discovery Schemas (Cascading Dropdowns)
# =============================================================================

class ProvinceItem(BaseModel):
    """Single province with AI-friendly coverage summary."""
    model_config = ConfigDict(populate_by_name=True)

    province: str
    district_count: int = Field(..., alias="districtCount")
    postal_city_count: int = Field(..., alias="postalCityCount")
    branch_count: int = Field(..., alias="branchCount")
    coverage_summary: str = Field(
        ...,
        alias="coverageSummary",
        description="AI-readable summary of branch coverage in this province",
    )


class ProvinceListResponse(BaseModel):
    """Response for GET /locations/provinces."""
    success: bool = True
    data: list[ProvinceItem]
    total: int


class DistrictItem(BaseModel):
    """Single district with coverage metadata."""
    model_config = ConfigDict(populate_by_name=True)

    district: str
    province: str
    postal_city_count: int = Field(..., alias="postalCityCount")
    branch_names: list[str] = Field(..., alias="branchNames")
    coverage_summary: str = Field(
        ...,
        alias="coverageSummary",
        description="AI-readable summary of branch coverage in this district",
    )


class DistrictListResponse(BaseModel):
    """Response for GET /locations/districts."""
    success: bool = True
    data: list[DistrictItem]
    total: int


class PostalCityItem(BaseModel):
    """Single postal city with its mapped branch details."""
    model_config = ConfigDict(populate_by_name=True)

    postal_city: str = Field(..., alias="postalCity")
    district: str
    province: str
    branch_id: UUID = Field(..., alias="branchId")
    branch_name: str = Field(..., alias="branchName")
    is_active: bool = Field(..., alias="isActive")


class PostalCityListResponse(BaseModel):
    """Response for GET /locations/postal-cities."""
    success: bool = True
    data: list[PostalCityItem]
    total: int


# =============================================================================
# Triplet-Based Branch Resolution (Direct Location Selection)
# =============================================================================

class LocationResolveRequest(BaseModel):
    """Resolve branch using a province / district / postal city triplet."""
    model_config = ConfigDict(populate_by_name=True)

    province: str = Field(..., min_length=1, max_length=100)
    district: str = Field(..., min_length=1, max_length=100)
    postal_city: str = Field(
        ...,
        min_length=1,
        max_length=100,
        validation_alias=POSTAL_CITY_ALIASES,
    )

    @field_validator("postal_city")
    @classmethod
    def normalize_postal_city(cls, v: str) -> str:
        return v.strip().title()

    @field_validator("province", "district")
    @classmethod
    def normalize_location(cls, v: str) -> str:
        return v.strip().title()


class LocationResolveResponse(BaseModel):
    """Response after resolving a location triplet to a branch."""
    success: bool = True
    data: dict = Field(
        ...,
        description="Branch context containing branchId, branchName, deliveryInfo, resolvedAt",
    )
    message: str


# =============================================================================
# Admin Mapping Management Schemas
# =============================================================================

class AdminMappingCreateRequest(PostalCityBranchMappingBase):
    """Admin request to create a new mapping — province & district required."""
    province: str = Field(..., min_length=1, max_length=100)
    district: str = Field(..., min_length=1, max_length=100)
    is_active: bool = True

    @field_validator("province", "district")
    @classmethod
    def normalize_location(cls, v: str) -> str:
        return v.strip().title()


class AdminMappingUpdateRequest(PostalCityBranchMappingUpdate):
    """Admin request to update an existing mapping — all fields optional."""

    @field_validator("province", "district")
    @classmethod
    def normalize_location(cls, v: Optional[str]) -> Optional[str]:
        if v is not None:
            return v.strip().title()
        return v


# =============================================================================
# Postal City Directory (Master List / "Delivery Zones") Schemas
# =============================================================================

class PostalCityDirectoryCreate(BaseModel):
    """Create a master postal city directory entry — Super Admin only."""
    model_config = ConfigDict(populate_by_name=True)

    postal_city: str = Field(..., min_length=1, max_length=100, validation_alias=POSTAL_CITY_ALIASES)
    district: str = Field(..., min_length=1, max_length=100)
    province: str = Field(..., min_length=1, max_length=100)
    is_active: bool = True

    @field_validator("postal_city", "district", "province")
    @classmethod
    def normalize(cls, v: str) -> str:
        return v.strip().title()


class PostalCityDirectoryUpdate(BaseModel):
    """Update a master postal city directory entry — all fields optional."""
    model_config = ConfigDict(populate_by_name=True)

    postal_city: Optional[str] = Field(
        None, min_length=1, max_length=100, validation_alias=POSTAL_CITY_ALIASES
    )
    district: Optional[str] = Field(None, min_length=1, max_length=100)
    province: Optional[str] = Field(None, min_length=1, max_length=100)
    is_active: Optional[bool] = None

    @field_validator("postal_city", "district", "province")
    @classmethod
    def normalize(cls, v: Optional[str]) -> Optional[str]:
        if v is not None:
            return v.strip().title()
        return v
