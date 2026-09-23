"""
Admin Support Ticket Management API

Branch-scoped ticket operations.
- Super Admin: sees all support requests across all branches with optional branch filtering.
- Branch Manager: scoped strictly to their assigned branch.
- Customer Support: scoped to their assigned branch (or unscoped if no branch assigned).

Prefix "/admin/support" is applied by app/api/v1/router.py.
"""
from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from loguru import logger
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.database import get_db
from app.core.dependencies import get_current_admin, require_permission
from app.models.admin import Admin
from app.models.support_ticket import SupportTicket, TicketStatus
from app.schemas.support_ticket import (
    SupportTicketListResponse,
    SupportTicketResponse,
    UpdateSupportTicketRequest,
)

router = APIRouter(
    dependencies=[Depends(require_permission("support", "read"))],
    tags=["Admin Support Tickets"],
)


def _format_ticket(ticket: SupportTicket) -> SupportTicketResponse:
    branch_name = ticket.branch.name if ticket.branch else None
    assigned_admin_name = ticket.assigned_admin.full_name if ticket.assigned_admin else None

    return SupportTicketResponse(
        ticket_id=str(ticket.ticket_id),
        ticket_number=ticket.ticket_number,
        user_id=str(ticket.user_id) if ticket.user_id else None,
        branch_id=str(ticket.branch_id) if ticket.branch_id else None,
        branch_name=branch_name,
        contact_phone=ticket.contact_phone,
        customer_name=ticket.customer_name,
        category=ticket.category,
        message=ticket.message,
        status=ticket.status,
        assigned_admin_id=str(ticket.assigned_admin_id) if ticket.assigned_admin_id else None,
        assigned_admin_name=assigned_admin_name,
        resolution_notes=ticket.resolution_notes,
        created_at=ticket.created_at.isoformat() if ticket.created_at else "",
        updated_at=ticket.updated_at.isoformat() if ticket.updated_at else "",
        resolved_at=ticket.resolved_at.isoformat() if ticket.resolved_at else None,
    )


def _resolve_admin_branch_scope(
    admin: Admin, requested_branch_id: Optional[UUID] = None
) -> Optional[UUID]:
    role = admin.role.value if hasattr(admin.role, "value") else admin.role
    if role == "super_admin":
        return requested_branch_id
    if admin.branch_id is not None:
        return admin.branch_id
    return requested_branch_id


@router.get(
    "/tickets",
    response_model=SupportTicketListResponse,
    summary="List support tickets with branch scoping and status filtering",
)
async def list_support_tickets(
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    status_filter: Optional[str] = Query(None, alias="status"),
    branch_id: Optional[UUID] = Query(None),
    search: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_db),
    admin: Admin = Depends(get_current_admin),
):
    target_branch_id = _resolve_admin_branch_scope(admin, branch_id)

    # Base query for counts and filtering
    base_conditions = []
    if target_branch_id:
        base_conditions.append(SupportTicket.branch_id == target_branch_id)

    # Count pending, in_progress, resolved for quick summary
    count_stmt = select(
        func.count().filter(SupportTicket.status == TicketStatus.PENDING.value),
        func.count().filter(SupportTicket.status == TicketStatus.IN_PROGRESS.value),
        func.count().filter(SupportTicket.status == TicketStatus.RESOLVED.value),
    )
    if base_conditions:
        count_stmt = count_stmt.where(*base_conditions)
    counts_row = (await db.execute(count_stmt)).first()
    pending_count = counts_row[0] if counts_row else 0
    in_progress_count = counts_row[1] if counts_row else 0
    resolved_count = counts_row[2] if counts_row else 0

    # Build filtered query
    query_conditions = list(base_conditions)
    if status_filter and status_filter.lower() != "all":
        query_conditions.append(SupportTicket.status == status_filter.lower())

    if search:
        search_term = f"%{search.strip()}%"
        query_conditions.append(
            or_(
                SupportTicket.ticket_number.ilike(search_term),
                SupportTicket.contact_phone.ilike(search_term),
                SupportTicket.customer_name.ilike(search_term),
                SupportTicket.message.ilike(search_term),
            )
        )

    # Total count for pagination
    total_stmt = select(func.count(SupportTicket.ticket_id))
    if query_conditions:
        total_stmt = total_stmt.where(*query_conditions)
    total = (await db.execute(total_stmt)).scalar() or 0

    # Query items
    items_stmt = (
        select(SupportTicket)
        .order_by(SupportTicket.created_at.desc())
        .offset((page - 1) * limit)
        .limit(limit)
    )
    if query_conditions:
        items_stmt = items_stmt.where(*query_conditions)

    result = await db.execute(items_stmt)
    tickets = result.scalars().all()

    total_pages = (total + limit - 1) // limit if limit > 0 else 0

    return SupportTicketListResponse(
        items=[_format_ticket(t) for t in tickets],
        total=total,
        page=page,
        limit=limit,
        total_pages=total_pages,
        pending_count=pending_count,
        in_progress_count=in_progress_count,
        resolved_count=resolved_count,
    )


@router.get(
    "/tickets/{ticket_id}",
    response_model=SupportTicketResponse,
    summary="Get single support ticket details",
)
async def get_support_ticket(
    ticket_id: UUID,
    db: AsyncSession = Depends(get_db),
    admin: Admin = Depends(get_current_admin),
):
    stmt = select(SupportTicket).where(SupportTicket.ticket_id == ticket_id)
    ticket = (await db.execute(stmt)).scalar_one_or_none()
    if not ticket:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Support ticket not found")

    role = admin.role.value if hasattr(admin.role, "value") else admin.role
    if role != "super_admin" and admin.branch_id and ticket.branch_id != admin.branch_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have access to support tickets from other branches",
        )

    return _format_ticket(ticket)


@router.patch(
    "/tickets/{ticket_id}",
    response_model=SupportTicketResponse,
    dependencies=[Depends(require_permission("support", "update"))],
    summary="Update support ticket status, assigned admin, and resolution notes",
)
async def update_support_ticket(
    ticket_id: UUID,
    payload: UpdateSupportTicketRequest,
    db: AsyncSession = Depends(get_db),
    admin: Admin = Depends(get_current_admin),
):
    stmt = select(SupportTicket).where(SupportTicket.ticket_id == ticket_id)
    ticket = (await db.execute(stmt)).scalar_one_or_none()
    if not ticket:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Support ticket not found")

    role = admin.role.value if hasattr(admin.role, "value") else admin.role
    if role != "super_admin" and admin.branch_id and ticket.branch_id != admin.branch_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You cannot update support tickets from other branches",
        )

    if payload.status:
        valid_statuses = {s.value for s in TicketStatus}
        if payload.status.lower() not in valid_statuses:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid status. Must be one of: {', '.join(valid_statuses)}",
            )
        ticket.status = payload.status.lower()
        if ticket.status in (TicketStatus.RESOLVED.value, TicketStatus.CLOSED.value):
            ticket.resolved_at = datetime.now(timezone.utc)

    if payload.assigned_admin_id is not None:
        ticket.assigned_admin_id = payload.assigned_admin_id

    if payload.resolution_notes is not None:
        ticket.resolution_notes = payload.resolution_notes.strip()

    ticket.updated_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(ticket)

    logger.info(f"Support ticket {ticket.ticket_number} updated by admin {admin.admin_id} (status: {ticket.status})")
    return _format_ticket(ticket)
