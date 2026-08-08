"""
SRIBEESonline - Partner Schemas
"""
from pydantic import BaseModel, EmailStr, Field


class PartnerLoginRequest(BaseModel):
    """Credentials for the partner portal. A separate realm from admin login."""

    email: EmailStr
    password: str = Field(..., min_length=1, max_length=128)
