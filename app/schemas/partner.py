"""
SRIBEESonline - Partner Schemas
"""
from typing import Optional
from uuid import UUID

from pydantic import BaseModel, EmailStr, Field


class PartnerLoginRequest(BaseModel):
    """Credentials for the partner portal. A separate realm from admin login."""

    email: EmailStr
    password: str = Field(..., min_length=1, max_length=128)


class PartnerForgotPasswordRequest(BaseModel):
    """Step 1 of the reset. ``email`` is a plain string so a malformed one
    answers 400 with the portal's message rather than a 422."""

    email: str = Field(..., max_length=255)


class PartnerVerifyOtpRequest(BaseModel):
    email: str = Field(..., max_length=255)
    otp: str = Field(..., max_length=12)


class PartnerResetPasswordRequest(BaseModel):
    """Step 3. Proof is ``reset_token`` (from verify-otp) or the ``otp``."""

    email: str = Field(..., max_length=255)
    otp: Optional[str] = Field(None, max_length=12)
    reset_token: Optional[str] = Field(None, max_length=128)
    new_password: str = Field(..., max_length=256)
    confirm_password: str = Field(..., max_length=256)


class PartnerCreateRequest(BaseModel):
    """
    Create a partner account.

    Shared by two callers: the admin panel (POST /admin/partners) and the
    partner portal's recruit endpoint (POST /partner/recruits). Neither
    accepts ``parent_partner_id`` from the client — the admin route always
    creates a top-level partner, and the recruit route always sets it to the
    authenticated caller server-side.
    """

    email: EmailStr
    password: str = Field(..., min_length=8, max_length=128)
    full_name: str = Field(..., min_length=2, max_length=150)
    phone: Optional[str] = Field(None, max_length=20)


class PartnerUpdateRequest(BaseModel):
    """
    Admin edit of a partner — all fields optional (partial update).

    Deliberately excludes ``parent_partner_id``: that column is write-once,
    set only at creation.
    """

    full_name: Optional[str] = Field(None, min_length=2, max_length=150)
    phone: Optional[str] = Field(None, max_length=20)
    password: Optional[str] = Field(None, min_length=8, max_length=128)
    is_active: Optional[bool] = None


class PartnerMarkPaidRequest(BaseModel):
    """
    Record out-of-band settlement of a partner's claimable commissions.

    ``commission_ids`` omitted or empty means "every claimable commission
    this partner currently has."
    """

    commission_ids: Optional[list[UUID]] = None
