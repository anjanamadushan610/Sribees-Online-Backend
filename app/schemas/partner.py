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
