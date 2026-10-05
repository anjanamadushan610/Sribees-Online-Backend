"""
SRIBEESonline FastAPI Backend - Auth Schemas

Pydantic models for authentication request/response validation.
"""
from datetime import datetime
from typing import Literal, Optional
from uuid import UUID

from pydantic import BaseModel, EmailStr, Field, field_validator

# ============================================================================
# Request Schemas
# ============================================================================

class RefreshTokenRequest(BaseModel):
    """Refresh token request schema."""

    refresh_token: str = Field(..., description="JWT refresh token")

    class Config:
        json_schema_extra = {
            "example": {
                "refreshToken": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...",
            }
        }


class UpdateProfileRequest(BaseModel):
    """Profile update request schema (PATCH /auth/me).

    The user's name is a single `full_name` field, matching the
    `users.full_name` database column. Only provided fields are updated.
    """

    full_name: Optional[str] = Field(
        None,
        min_length=2,
        max_length=100,
        description="User's full name",
    )
    email: Optional[EmailStr] = Field(
        None,
        description="Email address. Required for phone-only accounts before they can "
        "use a card/bank payment method -- checked for uniqueness against other accounts.",
    )
    phone: Optional[str] = Field(None, max_length=20, description="Phone number")
    nic: Optional[str] = Field(
        None,
        max_length=20,
        description="Sri Lankan NIC. Write-once: accepted while the account "
        "has none, rejected afterwards (support changes it).",
    )
    alternate_phone: Optional[str] = Field(
        None,
        max_length=20,
        description="A second contact number for deliveries. Send an empty "
        "string to remove it.",
    )
    profile_picture_url: Optional[str] = Field(
        None,
        max_length=500,
        description="Profile picture URL",
    )

    @field_validator("full_name")
    @classmethod
    def validate_full_name(cls, v: Optional[str]) -> Optional[str]:
        """Strip whitespace from name."""
        return v.strip() if v is not None else v

    @field_validator("nic")
    @classmethod
    def validate_nic(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        from app.utils.identity import normalize_nic

        return normalize_nic(v)

    @field_validator("alternate_phone")
    @classmethod
    def validate_alternate_phone(cls, v: Optional[str]) -> Optional[str]:
        # An empty string is how the app says "remove the second number".
        if v is None or not v.strip():
            return v
        from app.utils.identity import normalize_mobile

        return normalize_mobile(v)

    class Config:
        json_schema_extra = {
            "example": {
                "full_name": "John Doe",
                "phone": "+94771234567",
            }
        }


# ============================================================================
# Response Schemas
# ============================================================================

class RequestOTPResponse(BaseModel):
    """
    Response after requesting a phone OTP.

    Everything the code screen draws comes from here -- the number of boxes,
    the resend countdown, where the code went -- so the provider or the
    policy can change without an app release.
    """
    success: bool = True
    message: str = "OTP sent"
    expires_in_seconds: int = Field(..., alias="expiresInSeconds")
    resend_in_seconds: int = Field(60, alias="resendInSeconds")
    code_length: int = Field(6, alias="codeLength")
    destination: str = Field("", description="The number the code went to, masked")
    channel: str = Field("sms", description="How the code travels")

    class Config:
        populate_by_name = True

    @classmethod
    def from_challenge(cls, challenge) -> "RequestOTPResponse":
        return cls(
            message=challenge.message,
            expires_in_seconds=challenge.expires_in_seconds,
            resend_in_seconds=challenge.resend_in_seconds,
            code_length=challenge.code_length,
            destination=challenge.destination,
        )


class VerifyOTPRequest(BaseModel):
    """Request to verify a phone OTP."""
    code: str = Field(..., min_length=4, max_length=8)

    @field_validator("code")
    @classmethod
    def digits_only(cls, v: str) -> str:
        v = v.strip()
        if not v.isdigit():
            raise ValueError("OTP code must be digits only")
        return v


def _sri_lankan_mobile(v: str) -> str:
    from app.utils.identity import normalize_mobile

    return normalize_mobile(v)


class RequestPhoneOTPRequest(BaseModel):
    """Request to send a registration OTP to a phone number (no account yet)."""

    phone: str = Field(..., min_length=8, max_length=20, description="Phone number, e.g. +94771234567")

    @field_validator("phone")
    @classmethod
    def sri_lankan_mobile(cls, v: str) -> str:
        # Only a real Sri Lankan mobile gets a code: an SMS to any string sent
        # here would be paid for (and SMS-pumping fraud lives on exactly that).
        return _sri_lankan_mobile(v)

    class Config:
        json_schema_extra = {
            "example": {"phone": "+94771234567"},
        }


class VerifyPhoneOTPRequest(BaseModel):
    """Request to verify a registration OTP and log in/register by phone."""

    phone: str = Field(..., min_length=8, max_length=20)
    code: str = Field(..., min_length=4, max_length=8)

    @field_validator("phone")
    @classmethod
    def sri_lankan_mobile(cls, v: str) -> str:
        return _sri_lankan_mobile(v)

    @field_validator("code")
    @classmethod
    def digits_only(cls, v: str) -> str:
        v = v.strip()
        if not v.isdigit():
            raise ValueError("OTP code must be digits only")
        return v


class VerifyOTPResponse(BaseModel):
    """Response after verifying a phone OTP."""
    success: bool = True
    is_phone_verified: bool = Field(..., alias="isPhoneVerified")
    message: str = "Phone verified"

    class Config:
        populate_by_name = True


class UserResponse(BaseModel):
    """User data response schema."""

    user_id: UUID = Field(..., alias="userId")
    email: Optional[EmailStr] = None
    full_name: str = Field(..., alias="fullName")
    phone: Optional[str] = None
    nic: Optional[str] = None
    alternate_phone: Optional[str] = Field(None, alias="alternatePhone")
    profile_picture_url: Optional[str] = Field(None, alias="profilePictureUrl")
    is_verified: bool = Field(..., alias="isVerified")
    is_phone_verified: bool = Field(False, alias="isPhoneVerified")
    two_factor_enabled: bool = Field(False, alias="twoFactorEnabled")
    created_at: datetime = Field(..., alias="createdAt")
    updated_at: datetime = Field(..., alias="updatedAt")
    last_login: Optional[datetime] = Field(None, alias="lastLogin")

    class Config:
        from_attributes = True
        populate_by_name = True
        json_schema_extra = {
            "example": {
                "userId": "550e8400-e29b-41d4-a716-446655440000",
                "email": "user@example.com",
                "fullName": "John Doe",
                "phone": "+1234567890",
                "profilePictureUrl": "https://example.com/avatar.jpg",
                "isVerified": True,
                "twoFactorEnabled": False,
                "createdAt": "2026-01-30T10:00:00Z",
                "updatedAt": "2026-01-30T10:00:00Z",
                "lastLogin": "2026-01-30T15:30:00Z",
            }
        }


class TokensResponse(BaseModel):
    """JWT tokens response schema."""

    access_token: str = Field(..., alias="accessToken")
    refresh_token: str = Field(..., alias="refreshToken")

    class Config:
        populate_by_name = True
        json_schema_extra = {
            "example": {
                "accessToken": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...",
                "refreshToken": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...",
            }
        }


class AuthResponse(BaseModel):
    """Successful authentication response schema."""

    success: bool = True
    message: str
    user: UserResponse
    tokens: TokensResponse

    class Config:
        json_schema_extra = {
            "example": {
                "success": True,
                "message": "Login successful",
                "user": {
                    "userId": "550e8400-e29b-41d4-a716-446655440000",
                    "email": "user@example.com",
                    "fullName": "John Doe",
                    "isVerified": True,
                    "twoFactorEnabled": False,
                    "createdAt": "2026-01-30T10:00:00Z",
                    "updatedAt": "2026-01-30T10:00:00Z",
                },
                "tokens": {
                    "accessToken": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...",
                    "refreshToken": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...",
                }
            }
        }


class MessageResponse(BaseModel):
    """Simple message response schema."""

    success: bool = True
    message: str

    class Config:
        json_schema_extra = {
            "example": {
                "success": True,
                "message": "Operation completed successfully",
            }
        }


class RefreshTokenResponse(BaseModel):
    """Token refresh response schema."""

    success: bool = True
    message: str = "Token refreshed successfully"
    tokens: TokensResponse

    class Config:
        json_schema_extra = {
            "example": {
                "success": True,
                "message": "Token refreshed successfully",
                "tokens": {
                    "accessToken": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...",
                    "refreshToken": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...",
                }
            }
        }


class SessionResponse(BaseModel):
    """Session info response schema."""

    session_id: str = Field(..., alias="sessionId")
    device: Optional[str] = None
    ip_address: Optional[str] = Field(None, alias="ipAddress")
    user_agent: Optional[str] = Field(None, alias="userAgent")
    created_at: Optional[datetime] = Field(None, alias="createdAt")
    last_active: Optional[datetime] = Field(None, alias="lastActive")
    is_current: bool = Field(False, alias="isCurrent")

    class Config:
        from_attributes = True
        populate_by_name = True


class SessionsListResponse(BaseModel):
    """List of sessions response."""

    success: bool = True
    sessions: list[SessionResponse]


class ProfileResponse(BaseModel):
    """User profile response."""

    success: bool = True
    user: UserResponse


class ConfirmAccountDeletionRequest(BaseModel):
    """Closing an account: the code, and optionally why."""

    code: str = Field(..., min_length=4, max_length=8)
    reason: Optional[str] = Field(
        default=None,
        max_length=500,
        description="Why they left. Asked once, never required.",
    )


class SocialLoginRequest(BaseModel):
    """An ID token from Google or Apple, verified on the server.

    The token is the only thing trusted: identity and email come out of its
    verified claims, never from the request body. ``nonce`` is the raw value
    whose SHA-256 the app put into the sign-in request; when the token carries
    a nonce it must match, so a token lifted from another sign-in is useless.
    ``full_name`` is only a display-name hint (Apple puts no name in its token
    and hands it to the app once); it never identifies anyone.
    """

    provider: Literal["google", "apple"]
    id_token: str = Field(..., min_length=20, max_length=8192)
    nonce: Optional[str] = Field(default=None, max_length=256)
    full_name: Optional[str] = Field(default=None, max_length=200)


class PhoneChangeStatusResponse(BaseModel):
    """Quota info for primary phone changes."""

    success: bool = True
    daily_remaining: int = Field(..., alias="dailyRemaining")
    monthly_remaining: int = Field(..., alias="monthlyRemaining")
    daily_limit: int = Field(2, alias="dailyLimit")
    monthly_limit: int = Field(4, alias="monthlyLimit")
    can_change: bool = Field(..., alias="canChange")

    class Config:
        populate_by_name = True


class PhoneChangeRequest(BaseModel):
    """Request to change primary phone number."""

    new_phone: str = Field(..., max_length=20, alias="newPhone", description="New Sri Lankan mobile number")

    class Config:
        populate_by_name = True


class PhoneChangeVerifyRequest(BaseModel):
    """Verification for primary phone number change."""

    new_phone: str = Field(..., max_length=20, alias="newPhone")
    otp: str = Field(..., min_length=4, max_length=8)

    class Config:
        populate_by_name = True

