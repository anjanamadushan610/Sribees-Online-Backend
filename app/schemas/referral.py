"""
SRIBEESonline - Referral Schemas
"""
from pydantic import BaseModel, Field


class RedeemReferralRequest(BaseModel):
    """Attach the caller to the owner of an invite code."""

    referral_code: str = Field(
        ...,
        min_length=4,
        max_length=12,
        description="The inviting user's code. Case-insensitive.",
    )
