"""
SRIBEESonline - Branch & Post Office Mapping Schemas

Pydantic schemas for branch routing request/response validation.
"""
from datetime import datetime
from typing import Optional
from uuid import UUID

from pydantic import BaseModel, Field, field_validator, model_validator

# =============================================================================
# Branch Schemas
# =============================================================================

class BranchBase(BaseModel):
    """Base branch fields."""
    name: str = Field(..., min_length=1, max_length=255)
    code: str = Field(..., min_length=2, max_length=50)
    address: Optional[str] = None
    post_office: Optional[str] = Field(None, max_length=100)
    district: Optional[str] = Field(None, max_length=100)
    province: str = Field(..., max_length=100)
    phone: Optional[str] = Field(None, max_length=20)


class BranchCreate(BranchBase):
    """Create branch request."""
    manager_id: Optional[UUID] = None
    coverage_post_offices: Optional[list[str]] = Field(
        None,
        description=(
            "Post Office names this branch serves. Synced to "
            "PostOfficeBranchMapping in the same transaction."
        ),
    )


class BranchUpdate(BaseModel):
    """Update branch request — all fields optional."""
    name: Optional[str] = Field(None, min_length=1, max_length=255)
    code: Optional[str] = Field(None, min_length=2, max_length=50)
    address: Optional[str] = None
    post_office: Optional[str] = Field(None, max_length=100)
    district: Optional[str] = Field(None, max_length=100)
    province: Optional[str] = Field(None, max_length=100)
    phone: Optional[str] = Field(None, max_length=20)
    manager_id: Optional[UUID] = None
    is_active: Optional[bool] = None
    coverage_post_offices: Optional[list[str]] = Field(
        None,
        description=(
            "Full replacement set of Post Offices this branch serves. When "
            "provided, the branch's PostOfficeBranchMapping rows are synced to "
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
    branch_id: UUID = Field(..., alias="branchId")
    name: str
    code: str
    address: Optional[str] = None
    post_office: Optional[str] = Field(None, alias="postOffice")
    district: Optional[str] = None
    province: str
    phone: Optional[str] = None
    manager_id: Optional[UUID] = Field(None, alias="managerId")
    is_active: bool = Field(..., alias="isActive")
    created_at: datetime = Field(..., alias="createdAt")
    updated_at: datetime = Field(..., alias="updatedAt")

    class Config:
        from_attributes = True
        populate_by_name = True


# =============================================================================
# Post Office → Branch Mapping Schemas
# =============================================================================

class PostOfficeBranchMappingBase(BaseModel):
    """Base mapping fields."""
    post_office: str = Field(
        ...,
        min_length=1,
        max_length=100,
        description="Post Office name (must match addresses.post_office)",
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

    @field_validator("post_office")
    @classmethod
    def normalize_post_office(cls, v: str) -> str:
        """Normalize post office name: strip whitespace, title case."""
        return v.strip().title()


class PostOfficeBranchMappingCreate(PostOfficeBranchMappingBase):
    """Create mapping request."""
    is_active: bool = True


class PostOfficeBranchMappingUpdate(BaseModel):
    """Update mapping request — all fields optional."""
    post_office: Optional[str] = Field(None, min_length=1, max_length=100)
    branch_id: Optional[UUID] = None
    branch_name: Optional[str] = Field(None, min_length=1, max_length=255)
    district: Optional[str] = Field(None, max_length=100)
    province: Optional[str] = Field(None, max_length=100)
    is_active: Optional[bool] = None

    @field_validator("post_office")
    @classmethod
    def normalize_post_office(cls, v: Optional[str]) -> Optional[str]:
        if v is not None:
            return v.strip().title()
        return v


class PostOfficeBranchMappingResponse(BaseModel):
    """Mapping response."""
    mapping_id: UUID = Field(..., alias="mappingId")
    post_office: str = Field(..., alias="postOffice")
    branch_id: UUID = Field(..., alias="branchId")
    branch_name: str = Field(..., alias="branchName")
    district: Optional[str] = None
    province: Optional[str] = None
    is_active: bool = Field(..., alias="isActive")
    created_at: datetime = Field(..., alias="createdAt")
    updated_at: datetime = Field(..., alias="updatedAt")

    class Config:
        from_attributes = True
        populate_by_name = True


class PostOfficeBranchMappingListResponse(BaseModel):
    """List of mappings response."""
    success: bool = True
    mappings: list[PostOfficeBranchMappingResponse]
    total: int


# =============================================================================
# Branch Resolution Schemas (Customer-facing)
# =============================================================================

class BranchResolveRequest(BaseModel):
    """Request to resolve a delivery address to a branch."""
    address_id: UUID = Field(..., alias="addressId", description="User's saved address UUID")

    class Config:
        populate_by_name = True


class BranchResolveResponse(BaseModel):
    """Response after resolving an address to a branch."""
    success: bool = True
    branch_id: UUID = Field(..., alias="branchId")
    branch_name: str = Field(..., alias="branchName")
    post_office: str = Field(..., alias="postOffice")
    resolved_at: datetime = Field(..., alias="resolvedAt")

    class Config:
        populate_by_name = True


class BranchContextResponse(BaseModel):
    """Current branch context from session."""
    success: bool = True
    branch_id: UUID = Field(..., alias="branchId")
    branch_name: str = Field(..., alias="branchName")
    post_office: str = Field(..., alias="postOffice")
    resolved_at: datetime = Field(..., alias="resolvedAt")

    class Config:
        populate_by_name = True


class BranchContextClearedResponse(BaseModel):
    """Response when branch context is cleared."""
    success: bool = True
    message: str = "Branch context cleared. Please select a delivery address."


class ServedPostOfficesResponse(BaseModel):
    """List of all served post offices."""
    success: bool = True
    post_offices: list[str]
    total: int


# =============================================================================
# Location Discovery Schemas (Cascading Dropdowns)
# =============================================================================

class ProvinceItem(BaseModel):
    """Single province with AI-friendly coverage summary."""
    province: str
    district_count: int = Field(..., alias="districtCount")
    post_office_count: int = Field(..., alias="postOfficeCount")
    branch_count: int = Field(..., alias="branchCount")
    coverage_summary: str = Field(
        ...,
        alias="coverageSummary",
        description="AI-readable summary of branch coverage in this province",
    )

    class Config:
        populate_by_name = True


class ProvinceListResponse(BaseModel):
    """Response for GET /locations/provinces."""
    success: bool = True
    data: list[ProvinceItem]
    total: int


class DistrictItem(BaseModel):
    """Single district with coverage metadata."""
    district: str
    province: str
    post_office_count: int = Field(..., alias="postOfficeCount")
    branch_names: list[str] = Field(..., alias="branchNames")
    coverage_summary: str = Field(
        ...,
        alias="coverageSummary",
        description="AI-readable summary of branch coverage in this district",
    )

    class Config:
        populate_by_name = True


class DistrictListResponse(BaseModel):
    """Response for GET /locations/districts."""
    success: bool = True
    data: list[DistrictItem]
    total: int


class PostOfficeItem(BaseModel):
    """Single post office with its mapped branch details."""
    post_office: str = Field(..., alias="postOffice")
    district: str
    province: str
    branch_id: UUID = Field(..., alias="branchId")
    branch_name: str = Field(..., alias="branchName")
    is_active: bool = Field(..., alias="isActive")

    class Config:
        populate_by_name = True


class PostOfficeListResponse(BaseModel):
    """Response for GET /locations/post-offices."""
    success: bool = True
    data: list[PostOfficeItem]
    total: int


# =============================================================================
# Triplet-Based Branch Resolution (Direct Location Selection)
# =============================================================================

class LocationResolveRequest(BaseModel):
    """Resolve branch using a province / district / post_office triplet."""
    province: str = Field(..., min_length=1, max_length=100)
    district: str = Field(..., min_length=1, max_length=100)
    post_office: str = Field(
        ...,
        min_length=1,
        max_length=100,
        alias="postOffice",
    )

    class Config:
        populate_by_name = True

    @field_validator("post_office")
    @classmethod
    def normalize_po(cls, v: str) -> str:
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

class AdminMappingCreateRequest(PostOfficeBranchMappingBase):
    """Admin request to create a new mapping — province & district required."""
    province: str = Field(..., min_length=1, max_length=100)
    district: str = Field(..., min_length=1, max_length=100)
    is_active: bool = True

    @field_validator("province", "district")
    @classmethod
    def normalize_location(cls, v: str) -> str:
        return v.strip().title()


class AdminMappingUpdateRequest(BaseModel):
    """Admin request to update an existing mapping — all fields optional."""
    post_office: Optional[str] = Field(None, min_length=1, max_length=100)
    branch_id: Optional[UUID] = None
    branch_name: Optional[str] = Field(None, min_length=1, max_length=255)
    district: Optional[str] = Field(None, max_length=100)
    province: Optional[str] = Field(None, max_length=100)
    is_active: Optional[bool] = None

    @field_validator("post_office")
    @classmethod
    def normalize_post_office(cls, v: Optional[str]) -> Optional[str]:
        if v is not None:
            return v.strip().title()
        return v

    @field_validator("province", "district")
    @classmethod
    def normalize_location(cls, v: Optional[str]) -> Optional[str]:
        if v is not None:
            return v.strip().title()
        return v


# =============================================================================
# Post Office Directory (Master List / "Delivery Zones") Schemas
# =============================================================================

class PostOfficeDirectoryCreate(BaseModel):
    """Create a master Post Office directory entry — Super Admin only."""
    post_office: str = Field(..., min_length=1, max_length=100)
    district: str = Field(..., min_length=1, max_length=100)
    province: str = Field(..., min_length=1, max_length=100)
    is_active: bool = True

    @field_validator("post_office", "district", "province")
    @classmethod
    def normalize(cls, v: str) -> str:
        return v.strip().title()


class PostOfficeDirectoryUpdate(BaseModel):
    """Update a master Post Office directory entry — all fields optional."""
    post_office: Optional[str] = Field(None, min_length=1, max_length=100)
    district: Optional[str] = Field(None, min_length=1, max_length=100)
    province: Optional[str] = Field(None, min_length=1, max_length=100)
    is_active: Optional[bool] = None

    @field_validator("post_office", "district", "province")
    @classmethod
    def normalize(cls, v: Optional[str]) -> Optional[str]:
        if v is not None:
            return v.strip().title()
        return v
