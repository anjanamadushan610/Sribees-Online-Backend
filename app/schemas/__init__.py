"""
SRIBEESonline FastAPI Backend - Schemas Module

Pydantic schemas for request/response validation.
"""
from app.schemas.auth import (
    AuthResponse,
    MessageResponse,
    RefreshTokenRequest,
    RefreshTokenResponse,
    TokensResponse,
    UserResponse,
)
from app.schemas.branch import (
    # Admin mapping management
    AdminMappingCreateRequest,
    AdminMappingUpdateRequest,
    BranchContextClearedResponse,
    BranchContextResponse,
    BranchCreate,
    BranchResolveRequest,
    BranchResolveResponse,
    BranchResponse,
    BranchUpdate,
    DistrictItem,
    DistrictListResponse,
    # Triplet-based resolution
    LocationResolveRequest,
    LocationResolveResponse,
    PostalCityBranchMappingCreate,
    PostalCityBranchMappingListResponse,
    PostalCityBranchMappingResponse,
    PostalCityBranchMappingUpdate,
    PostalCityItem,
    PostalCityListResponse,
    # Location discovery
    ProvinceItem,
    ProvinceListResponse,
    ServedPostalCitiesResponse,
)

__all__ = [
    # Auth requests
    "RefreshTokenRequest",
    # Auth responses
    "UserResponse",
    "TokensResponse",
    "AuthResponse",
    "MessageResponse",
    "RefreshTokenResponse",
    # Branch schemas
    "BranchCreate",
    "BranchUpdate",
    "BranchResponse",
    # Postal city mapping schemas
    "PostalCityBranchMappingCreate",
    "PostalCityBranchMappingUpdate",
    "PostalCityBranchMappingResponse",
    "PostalCityBranchMappingListResponse",
    # Branch resolution schemas
    "BranchResolveRequest",
    "BranchResolveResponse",
    "BranchContextResponse",
    "BranchContextClearedResponse",
    "ServedPostalCitiesResponse",
    # Location discovery schemas
    "ProvinceItem",
    "ProvinceListResponse",
    "DistrictItem",
    "DistrictListResponse",
    "PostalCityItem",
    "PostalCityListResponse",
    # Triplet-based resolution schemas
    "LocationResolveRequest",
    "LocationResolveResponse",
    # Admin mapping management schemas
    "AdminMappingCreateRequest",
    "AdminMappingUpdateRequest",
]
