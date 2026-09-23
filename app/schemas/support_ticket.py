"""
Pydantic schemas for Support Tickets.
"""
from datetime import datetime
from typing import Optional
from uuid import UUID

from pydantic import BaseModel, Field


class CreateSupportTicketRequest(BaseModel):
    contact_phone: str = Field(..., min_length=7, max_length=25, description="Contact phone number")
    message: str = Field(..., min_length=5, max_length=3000, description="Detailed problem/issue description")
    category: str = Field(default="general", max_length=50, description="Ticket category: general, order, delivery, app, payment")
    branch_id: Optional[UUID] = Field(default=None, description="Active branch ID if available")
    customer_name: Optional[str] = Field(default=None, max_length=255, description="Customer name if available")


class UpdateSupportTicketRequest(BaseModel):
    status: Optional[str] = Field(default=None, description="pending, in_progress, resolved, closed")
    assigned_admin_id: Optional[UUID] = Field(default=None, description="Admin to assign ticket to")
    resolution_notes: Optional[str] = Field(default=None, max_length=3000, description="Internal resolution remarks")


class SupportTicketResponse(BaseModel):
    ticket_id: str
    ticket_number: str
    user_id: Optional[str] = None
    branch_id: Optional[str] = None
    branch_name: Optional[str] = None
    contact_phone: str
    customer_name: Optional[str] = None
    category: str
    message: str
    status: str
    assigned_admin_id: Optional[str] = None
    assigned_admin_name: Optional[str] = None
    resolution_notes: Optional[str] = None
    created_at: str
    updated_at: str
    resolved_at: Optional[str] = None


class SupportTicketListResponse(BaseModel):
    items: list[SupportTicketResponse]
    total: int
    page: int
    limit: int
    total_pages: int
    pending_count: int = 0
    in_progress_count: int = 0
    resolved_count: int = 0
