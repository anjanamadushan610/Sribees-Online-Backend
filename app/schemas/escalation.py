"""Schemas for order escalation tickets (QA spec B4, Module B)."""
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field

from app.models.escalation import EscalationCategory, EscalationStatus


class EscalationCreate(BaseModel):
    category: EscalationCategory = EscalationCategory.OTHER
    message: str = Field(..., min_length=5, max_length=2000)


class EscalationUpdate(BaseModel):
    """Acknowledge or resolve — Branch Manager / Super Admin only."""
    status: EscalationStatus
    resolution_note: Optional[str] = Field(None, max_length=2000)


class EscalationOut(BaseModel):
    escalation_id: str
    order_id: str
    raised_by_admin_id: Optional[str] = None
    raised_by_role: Optional[str] = None
    category: str
    message: str
    status: str
    resolution_note: Optional[str] = None
    raised_by_name: str
    handled_by_name: Optional[str] = None
    created_at: Optional[datetime] = None
    acknowledged_at: Optional[datetime] = None
    resolved_at: Optional[datetime] = None
