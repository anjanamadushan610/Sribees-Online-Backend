"""
SRIBEESonline FastAPI Backend - Auth Schemas

Pydantic models for authentication request/response validation.
"""
from datetime import datetime
from typing import Optional
from uuid import UUID

from pydantic import BaseModel, EmailStr, Field, field_validator

# ============================================================================
# Request Schemas
# ============================================================================

class RegisterRequest(BaseModel):
    """User registration request schema."""

    email: EmailStr = Field(..., description="User's email address")
    password: str = Field(
        ...,
        min_length=8,
        max_length=128,
        description="Password (min 8 characters)",
    )
    full_name: str = Field(
        ...,
        min_length=2,
        max_length=100,
        description="User's full name",
    )
    referral_code: Optional[str] = Field(
        None,
        max_length=12,
        description=(
            "Invite code of the user who referred this signup. Optional, and "
            "a bad code never blocks registration — it is simply not applied."
        ),
    )

    @field_validator("password")
    @classmethod
    def validate_password(cls, v: str) -> str:
        """Validate password strength."""
        if len(v) < 8:
            raise ValueError("Password must be at least 8 characters")
        if not any(c.isupper() for c in v):
            raise ValueError("Password must contain at least one uppercase letter")
        if not any(c.islower() for c in v):
            raise ValueError("Password must contain at least one lowercase letter")
        if not any(c.isdigit() for c in v):
            raise ValueError("Password must contain at least one digit")
        return v

    @field_validator("full_name")
    @classmethod
    def validate_full_name(cls, v: str) -> str:
        """Strip whitespace from name."""
        return v.strip()

    class Config:
        json_schema_extra = {
            "example": {
                "email": "user@example.com",
                "password": "SecurePass123",
                "fullName": "John Doe",
            }
        }


class LoginRequest(BaseModel):
    """User login request schema."""

    email: EmailStr = Field(..., description="User's email address")
    password: str = Field(..., description="User's password")
    remember_me: bool = Field(
        default=False,
        description="Extend session to 30 days",
    )

    class Config:
        json_schema_extra = {
            "example": {
                "email": "user@example.com",
                "password": "SecurePass123",
                "rememberMe": False,
            }
        }


class VerifyEmailRequest(BaseModel):
    """Email verification request schema."""

    token: str = Field(..., description="Verification token from email")

    class Config:
        json_schema_extra = {
            "example": {
                "token": "abc123def456...",
            }
        }


class ResendVerificationRequest(BaseModel):
    """Resend verification email request schema."""

    email: EmailStr = Field(..., description="User's email address")

    class Config:
        json_schema_extra = {
            "example": {
                "email": "user@example.com",
            }
        }


class ForgotPasswordRequest(BaseModel):
    """Forgot password request schema."""

    email: EmailStr = Field(..., description="User's email address")

    class Config:
        json_schema_extra = {
            "example": {
                "email": "user@example.com",
            }
        }


class ResetPasswordRequest(BaseModel):
    """Reset password request schema."""

    token: str = Field(..., description="Password reset token from email")
    password: str = Field(
        ...,
        min_length=8,
        max_length=128,
        description="New password (min 8 characters)",
    )

    @field_validator("password")
    @classmethod
    def validate_password(cls, v: str) -> str:
        """Validate password strength."""
        if len(v) < 8:
            raise ValueError("Password must be at least 8 characters")
        if not any(c.isupper() for c in v):
            raise ValueError("Password must contain at least one uppercase letter")
        if not any(c.islower() for c in v):
            raise ValueError("Password must contain at least one lowercase letter")
        if not any(c.isdigit() for c in v):
            raise ValueError("Password must contain at least one digit")
        return v

    class Config:
        json_schema_extra = {
            "example": {
                "token": "abc123def456...",
                "password": "NewSecurePass123",
            }
        }


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


class ChangePasswordRequest(BaseModel):
    """Change password request schema."""

    current_password: str = Field(..., description="Current password")
    new_password: str = Field(
        ...,
        min_length=8,
        max_length=128,
        description="New password (min 8 characters)",
    )

    @field_validator("new_password")
    @classmethod
    def validate_password(cls, v: str) -> str:
        """Validate password strength."""
        if len(v) < 8:
            raise ValueError("Password must be at least 8 characters")
        if not any(c.isupper() for c in v):
            raise ValueError("Password must contain at least one uppercase letter")
        if not any(c.islower() for c in v):
            raise ValueError("Password must contain at least one lowercase letter")
        if not any(c.isdigit() for c in v):
            raise ValueError("Password must contain at least one digit")
        return v


# ============================================================================
# Response Schemas
# ============================================================================

class RequestOTPResponse(BaseModel):
    """Response after requesting a phone OTP."""
    success: bool = True
    message: str = "OTP sent"
    expires_in_seconds: int = Field(..., alias="expiresInSeconds")

    class Config:
        populate_by_name = True


class VerifyOTPRequest(BaseModel):
    """Request to verify a phone OTP."""
    code: str = Field(..., min_length=6, max_length=6)

    @field_validator("code")
    @classmethod
    def digits_only(cls, v: str) -> str:
        v = v.strip()
        if not v.isdigit():
            raise ValueError("OTP code must be 6 digits")
        return v


class RequestPhoneOTPRequest(BaseModel):
    """Request to send a registration OTP to a phone number (no account yet)."""

    phone: str = Field(..., min_length=8, max_length=20, description="Phone number, e.g. +94771234567")

    class Config:
        json_schema_extra = {
            "example": {"phone": "+94771234567"},
        }


class VerifyPhoneOTPRequest(BaseModel):
    """Request to verify a registration OTP and log in/register by phone."""

    phone: str = Field(..., min_length=8, max_length=20)
    code: str = Field(..., min_length=6, max_length=6)

    @field_validator("code")
    @classmethod
    def digits_only(cls, v: str) -> str:
        v = v.strip()
        if not v.isdigit():
            raise ValueError("OTP code must be 6 digits")
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


class RegisterResponse(BaseModel):
    """Registration response schema."""

    success: bool = True
    message: str
    user: UserResponse

    class Config:
        json_schema_extra = {
            "example": {
                "success": True,
                "message": "Registration successful. Please check your email to verify your account.",
                "user": {
                    "userId": "550e8400-e29b-41d4-a716-446655440000",
                    "email": "user@example.com",
                    "fullName": "John Doe",
                    "isVerified": False,
                    "createdAt": "2026-01-30T10:00:00Z",
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
    """A Firebase ID token from a Google or Apple sign-in.

    The token is the only thing trusted. Email, name and provider are read
    from its verified claims, never from the request body.
    """

    id_token: str = Field(..., min_length=20)
