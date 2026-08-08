"""
SRIBEESonline - AI Shopping Chat Schemas
"""
from typing import Optional

from pydantic import BaseModel, Field


class ChatMessageRequest(BaseModel):
    """One turn sent to the AI shopping assistant."""

    message: str = Field(..., min_length=1, max_length=1000)
    image_base64: Optional[str] = Field(
        None, description="Optional photo, base64-encoded (no data: prefix)."
    )
    image_mime_type: Optional[str] = Field(
        None, description="e.g. 'image/jpeg' — required if image_base64 is set."
    )
