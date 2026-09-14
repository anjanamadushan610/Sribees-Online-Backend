"""
User Address Pydantic Schemas

Request/response models for the /user/addresses CRUD endpoints.

An address is written in a *postal city* (see app/models/branch.py). Requests
also accept the pre-rename ``post_office`` spelling, and responses repeat the
value under it, because app builds already installed on customers' phones send
and read that key.
"""
from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.schemas.branch import POSTAL_CITY_ALIASES

# ============================================================================
# Request Schemas
# ============================================================================

class AddressCreateRequest(BaseModel):
    """Request to create a delivery address."""

    model_config = ConfigDict(
        populate_by_name=True,
        json_schema_extra={
            "example": {
                "address_line1": "42/A, Flower Road",
                "address_line2": "Welipenna",
                "postal_city": "Welipenna",
                "district": "Kalutara",
                "province": "Western Province",
                "postal_code": "12000",
                "is_default": True,
            }
        },
    )

    title: Optional[str] = Field(None, max_length=100, description="Label e.g. Home, Office")
    recipient_name: Optional[str] = Field(None, max_length=150)
    phone: Optional[str] = Field(None, max_length=30)
    address_line1: str = Field(..., min_length=1, max_length=255)
    address_line2: Optional[str] = Field(None, max_length=255)
    postal_city: str = Field(..., min_length=1, max_length=100, validation_alias=POSTAL_CITY_ALIASES)
    district: str = Field(..., min_length=1, max_length=100)
    province: str = Field(..., min_length=1, max_length=100)
    postal_code: Optional[str] = Field(None, max_length=20)
    is_default: bool = False

    @field_validator("address_line1", "postal_city", "district", "province")
    @classmethod
    def strip_whitespace(cls, v: str) -> str:
        return v.strip()


class AddressUpdateRequest(BaseModel):
    """Request to update a delivery address (partial)."""

    model_config = ConfigDict(populate_by_name=True)

    title: Optional[str] = Field(None, max_length=100)
    recipient_name: Optional[str] = Field(None, max_length=150)
    phone: Optional[str] = Field(None, max_length=30)
    address_line1: Optional[str] = Field(None, min_length=1, max_length=255)
    address_line2: Optional[str] = Field(None, max_length=255)
    postal_city: Optional[str] = Field(
        None, min_length=1, max_length=100, validation_alias=POSTAL_CITY_ALIASES
    )
    district: Optional[str] = Field(None, min_length=1, max_length=100)
    province: Optional[str] = Field(None, min_length=1, max_length=100)
    postal_code: Optional[str] = Field(None, max_length=20)
    is_default: Optional[bool] = None


# ============================================================================
# Response Schemas
# ============================================================================

class AddressResponse(BaseModel):
    """Address in API responses (snake_case, per mobile contract)."""

    address_id: str
    title: Optional[str] = None
    recipient_name: Optional[str] = None
    phone: Optional[str] = None
    address_line1: str
    address_line2: Optional[str] = None
    postal_city: str
    # Same value, pre-rename key — see module docstring.
    post_office: str
    district: str
    province: str
    postal_code: Optional[str] = None
    is_default: bool = False
    # Resolved delivery branch for this address (via postal city mapping).
    # branch_id is null / is_serviceable false when no branch serves the area.
    branch_id: Optional[str] = None
    branch_name: Optional[str] = None
    is_serviceable: bool = False
    # Whether SribeesExpress can be asked to price a delivery here: the postal
    # city matched their directory. A branch can serve an area the courier does
    # not reach yet, and such an address cannot be checked out — there is no
    # flat delivery fee to fall back to.
    delivery_available: bool = False


class AddressListResponse(BaseModel):
    """List of addresses wrapped in `data` (per mobile contract)."""

    success: bool = True
    data: List[AddressResponse]


class AddressDetailResponse(BaseModel):
    """Single address wrapped in `data`."""

    success: bool = True
    data: AddressResponse
    message: Optional[str] = None
