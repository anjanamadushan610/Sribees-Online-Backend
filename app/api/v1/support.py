"""
Customer Support Tickets API (Public / Customer facing)

Allows customers to submit support inquiries / issues from the mobile app.
Prefix: "/support" (mounted at /api/v1/support).
"""
import random
import string
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, status
from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.database import get_db
from app.core.dependencies import get_current_user_optional
from app.models.support_ticket import SupportTicket, TicketStatus
from app.models.user import Address, User
from app.schemas.support_ticket import CreateSupportTicketRequest

router = APIRouter(prefix="/support", tags=["Customer Support"])


def _generate_ticket_number() -> str:
    colombo_tz = ZoneInfo("Asia/Colombo")
    timestamp = datetime.now(colombo_tz).strftime("%Y%m%d")
    random_part = "".join(random.choices(string.ascii_uppercase + string.digits, k=5))
    return f"TKT-{timestamp}-{random_part}"


@router.post(
    "/tickets",
    status_code=status.HTTP_201_CREATED,
    summary="Submit a customer support ticket",
)
async def submit_support_ticket(
    payload: CreateSupportTicketRequest,
    db: AsyncSession = Depends(get_db),
    current_user: Optional[User] = Depends(get_current_user_optional),
):
    """
    Submit a customer support request/issue.

    Associates with logged-in user if available, and captures contact phone,
    issue description, and active branch.
    """
    user_id = current_user.user_id if current_user else None
    customer_name = payload.customer_name or (current_user.full_name if current_user else None)

    # Resolve branch if not explicitly provided
    branch_id = payload.branch_id
    if not branch_id and user_id:
        # Check default address of user for branch mapping
        stmt = (
            select(Address.branch_id)
            .where(Address.user_id == user_id, Address.is_deleted.is_(False))
            .order_by(Address.is_default.desc(), Address.created_at.desc())
            .limit(1)
        )
        res = await db.execute(stmt)
        branch_id = res.scalar_one_or_none()

    ticket_number = _generate_ticket_number()

    ticket = SupportTicket(
        ticket_number=ticket_number,
        user_id=user_id,
        branch_id=branch_id,
        contact_phone=payload.contact_phone.strip(),
        customer_name=customer_name,
        category=payload.category,
        message=payload.message.strip(),
        status=TicketStatus.PENDING.value,
    )
    db.add(ticket)
    await db.commit()
    await db.refresh(ticket)

    logger.info(
        f"Support ticket {ticket_number} created for phone {ticket.contact_phone} "
        f"(branch: {branch_id}, user: {user_id})"
    )

    return {
        "success": True,
        "ticket_id": str(ticket.ticket_id),
        "ticket_number": ticket.ticket_number,
        "message": "Your support request has been submitted successfully.",
    }
