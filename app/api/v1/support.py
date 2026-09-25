"""
Customer Support Tickets API (Public / Customer facing)

Allows customers to submit support inquiries / issues from the mobile app.
Prefix: "/support" (mounted at /api/v1/support).
"""
import secrets
import string
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Request, status
from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.database import get_db
from app.core.dependencies import get_current_user_optional
from app.models.support_ticket import SupportTicket, TicketStatus
from app.models.user import Address, User
from app.schemas.support_ticket import CreateSupportTicketRequest

router = APIRouter(prefix="/support", tags=["Customer Support"])


def _device_id(request: Request) -> Optional[str]:
    raw = (request.headers.get("X-Device-Id") or "").strip()
    return raw[:128] or None


def _phone_conditions(phone: str) -> list:
    phone = phone.strip()
    conditions = [SupportTicket.contact_phone == phone]
    digits = "".join(c for c in phone if c.isdigit())
    if len(digits) >= 9:
        conditions.append(SupportTicket.contact_phone.ilike(f"%{digits[-9:]}%"))
    return conditions


def _generate_ticket_number() -> str:
    colombo_tz = ZoneInfo("Asia/Colombo")
    timestamp = datetime.now(colombo_tz).strftime("%Y%m%d")
    alphabet = string.ascii_uppercase + string.digits
    random_part = "".join(secrets.choice(alphabet) for _ in range(5))
    return f"TKT-{timestamp}-{random_part}"


@router.post(
    "/tickets",
    status_code=status.HTTP_201_CREATED,
    summary="Submit a customer support ticket",
)
async def submit_support_ticket(
    payload: CreateSupportTicketRequest,
    request: Request,
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
        device_id=None if user_id else _device_id(request),
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


@router.get(
    "/my-tickets",
    summary="Get support tickets submitted by the current customer",
)
async def get_my_support_tickets(
    request: Request,
    phone: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    current_user: Optional[User] = Depends(get_current_user_optional),
):
    """
    Retrieve customer support tickets submitted by the user.

    A signed-in customer gets the tickets on their account and on their own
    phone number; the ``phone`` parameter is ignored for them. A guest gets
    only tickets filed from this install (X-Device-Id) under ``phone`` -- a
    phone number by itself is not proof of anything.
    """
    from sqlalchemy import and_, or_

    conditions = []
    if current_user:
        conditions.append(SupportTicket.user_id == current_user.user_id)
        if current_user.phone:
            conditions.extend(_phone_conditions(current_user.phone))
    else:
        device_id = _device_id(request)
        if device_id and phone and phone.strip():
            conditions.append(
                and_(SupportTicket.device_id == device_id, or_(*_phone_conditions(phone)))
            )

    if not conditions:
        return {"items": [], "total": 0}

    stmt = (
        select(SupportTicket)
        .where(or_(*conditions))
        .order_by(SupportTicket.created_at.desc())
        .limit(50)
    )
    res = await db.execute(stmt)
    tickets = res.scalars().all()

    items = []
    for t in tickets:
        branch_name = t.branch.name if t.branch else None
        items.append(
            {
                "ticket_id": str(t.ticket_id),
                "ticket_number": t.ticket_number,
                "category": t.category,
                "message": t.message,
                "status": t.status,
                "branch_id": str(t.branch_id) if t.branch_id else None,
                "branch_name": branch_name,
                "contact_phone": t.contact_phone,
                "resolution_notes": t.resolution_notes,
                "created_at": t.created_at.isoformat() if t.created_at else None,
                "updated_at": t.updated_at.isoformat() if t.updated_at else None,
                "resolved_at": t.resolved_at.isoformat() if t.resolved_at else None,
            }
        )

    return {"items": items, "total": len(items)}

