"""Ticket-scoped WhatsApp history and idempotent, queued staff replies."""

from datetime import datetime, timedelta, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select, text, tuple_, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.database import get_db
from app.config.settings import settings
from app.core.dependencies import get_current_admin, require_permission
from app.models.admin import Admin
from app.models.support_ticket import SupportTicket
from app.models.whatsapp import WhatsAppConversation, WhatsAppReply
from app.services.whatsapp_menu import text_reply

router = APIRouter(dependencies=[Depends(require_permission("support", "read"))])
write_access = Depends(require_permission("support", "update"))
ACTIVE_STATUSES = {"pending", "in_progress"}


class StaffReplyRequest(BaseModel):
    request_id: UUID
    body: str = Field(min_length=1, max_length=4096)


async def ticket_context(db, admin, ticket_id, *, lock=False):
    if lock:
        # Same lock order as inbound processing; serializes claim/send/MENU.
        await db.execute(text("SELECT pg_advisory_xact_lock(8767000836)"))
    ticket = await db.get(SupportTicket, ticket_id)
    if ticket is None:
        raise HTTPException(404, "Support ticket not found")
    role = admin.role.value if hasattr(admin.role, "value") else admin.role
    if (
        role != "super_admin"
        and admin.branch_id
        and admin.branch_id != ticket.branch_id
    ):
        raise HTTPException(403, "This ticket belongs to another branch")
    conversation = (
        await db.execute(
            select(WhatsAppConversation).where(
                WhatsAppConversation.ticket_id == ticket_id
            )
        )
    ).scalar_one_or_none()
    return ticket, conversation


def require_active(ticket, conversation):
    if conversation is None or ticket.status not in ACTIVE_STATUSES:
        raise HTTPException(
            409,
            "This WhatsApp conversation is no longer active. Ask the customer to send SUPPORT and describe their issue again.",
        )


def message_data(row):
    outgoing = None
    if row.payload:
        if row.payload.get("type") == "text":
            outgoing = row.payload.get("text", {}).get("body", "")
        elif row.payload.get("type") == "interactive":
            outgoing = (
                row.payload.get("interactive", {}).get("body", {}).get("text", "")
            )
    return {
        "id": str(row.reply_id),
        "incoming": row.inbound_text,
        "incoming_type": row.inbound_type,
        "has_attachment": bool(row.inbound_media),
        "received_at": row.received_at.isoformat() if row.received_at else None,
        "outgoing": outgoing,
        "is_staff": row.is_staff,
        "status": row.status,
        "error_code": row.error_code,
        "created_at": row.created_at.isoformat(),
    }


@router.get("/tickets/{ticket_id}/whatsapp")
async def history(
    ticket_id: UUID,
    before: UUID | None = None,
    limit: int = Query(50, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    admin: Admin = Depends(get_current_admin),
):
    ticket, conversation = await ticket_context(db, admin, ticket_id)
    query = select(WhatsAppReply).where(WhatsAppReply.ticket_id == ticket_id)
    if before:
        cursor = await db.get(WhatsAppReply, before)
        if cursor is None or cursor.ticket_id != ticket_id:
            raise HTTPException(400, "Invalid history cursor")
        query = query.where(
            tuple_(WhatsAppReply.created_at, WhatsAppReply.reply_id)
            < tuple_(cursor.created_at, cursor.reply_id)
        )
    rows = (
        (
            await db.execute(
                query.order_by(
                    WhatsAppReply.created_at.desc(), WhatsAppReply.reply_id.desc()
                ).limit(limit + 1)
            )
        )
        .scalars()
        .all()
    )
    has_more = len(rows) > limit
    rows = rows[:limit]
    closes_at = (
        conversation.last_message_at + timedelta(seconds=86100)
        if conversation
        else None
    )
    return {
        "available": conversation is not None or bool(rows),
        "active": conversation is not None and ticket.status in ACTIVE_STATUSES,
        "staff_active": bool(conversation and conversation.staff_active),
        "assigned_admin_id": str(ticket.assigned_admin_id)
        if ticket.assigned_admin_id
        else None,
        "reply_until": closes_at.isoformat() if closes_at else None,
        "enabled": settings.whatsapp_enabled,
        "language": getattr(ticket, "preferred_language", None),
        "related_order_id": str(ticket.related_order_id) if getattr(ticket, "related_order_id", None) else None,
        "staff_notification_status": getattr(ticket, "notification_status", None),
        "messages": [message_data(row) for row in reversed(rows)],
        "next_before": str(rows[-1].reply_id) if has_more else None,
    }


@router.post("/tickets/{ticket_id}/whatsapp/claim", dependencies=[write_access])
async def claim(
    ticket_id: UUID,
    db: AsyncSession = Depends(get_db),
    admin: Admin = Depends(get_current_admin),
):
    ticket, conversation = await ticket_context(db, admin, ticket_id, lock=True)
    require_active(ticket, conversation)
    if ticket.assigned_admin_id and ticket.assigned_admin_id != admin.admin_id:
        raise HTTPException(409, "This ticket is assigned to another staff member")
    ticket.assigned_admin_id = admin.admin_id
    ticket.status = "in_progress"
    conversation.staff_active = True
    await db.execute(
        update(WhatsAppReply)
        .where(
            WhatsAppReply.phone == conversation.phone,
            WhatsAppReply.status == "pending",
            WhatsAppReply.is_staff.is_(False),
        )
        .values(status="cancelled")
    )
    await db.commit()
    return {"claimed": True}


@router.post("/tickets/{ticket_id}/whatsapp/release", dependencies=[write_access])
async def release(
    ticket_id: UUID,
    db: AsyncSession = Depends(get_db),
    admin: Admin = Depends(get_current_admin),
):
    ticket, conversation = await ticket_context(db, admin, ticket_id, lock=True)
    require_active(ticket, conversation)
    if ticket.assigned_admin_id != admin.admin_id:
        raise HTTPException(
            409, "Only the assigned staff member can release this conversation"
        )
    conversation.staff_active = False
    conversation.ticket_id = None
    from app.services.whatsapp_support import reset
    reset(conversation)
    ticket.assigned_admin_id = None
    await db.execute(
        update(WhatsAppReply)
        .where(
            WhatsAppReply.ticket_id == ticket_id,
            WhatsAppReply.status == "pending",
            WhatsAppReply.is_staff.is_(True),
        )
        .values(status="cancelled")
    )
    await db.commit()
    return {"released": True}


@router.post("/tickets/{ticket_id}/whatsapp/messages", dependencies=[write_access])
async def send_reply(
    ticket_id: UUID,
    payload: StaffReplyRequest,
    db: AsyncSession = Depends(get_db),
    admin: Admin = Depends(get_current_admin),
):
    ticket, conversation = await ticket_context(db, admin, ticket_id, lock=True)
    body = payload.body.strip()
    if not body:
        raise HTTPException(422, "Enter a message")
    message_id = f"staff:{ticket_id}:{payload.request_id}"
    existing = (
        await db.execute(
            select(WhatsAppReply).where(WhatsAppReply.message_id == message_id)
        )
    ).scalar_one_or_none()
    if existing:
        if existing.admin_id != admin.admin_id or existing.payload != text_reply(body):
            raise HTTPException(409, "This request ID has already been used")
        return message_data(existing)
    require_active(ticket, conversation)
    if not settings.whatsapp_enabled or not settings.whatsapp_access_token:
        raise HTTPException(503, "WhatsApp sending is unavailable")
    if not conversation.staff_active or ticket.assigned_admin_id != admin.admin_id:
        raise HTTPException(409, "Take over this conversation before replying")
    expires_at = conversation.last_message_at + timedelta(seconds=86100)
    if expires_at <= datetime.now(timezone.utc):
        raise HTTPException(
            409,
            "The 24-hour reply window has ended. Wait for a new customer message or use an approved WhatsApp template.",
        )
    row = WhatsAppReply(
        message_id=message_id,
        phone=conversation.phone,
        ticket_id=ticket_id,
        admin_id=admin.admin_id,
        is_staff=True,
        payload=text_reply(body),
        status="pending",
        expires_at=expires_at,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return message_data(row)


@router.get("/tickets/{ticket_id}/whatsapp/messages/{message_id}/attachment")
async def attachment(
    ticket_id: UUID,
    message_id: UUID,
    db: AsyncSession = Depends(get_db),
    admin: Admin = Depends(get_current_admin),
):
    await ticket_context(db, admin, ticket_id)
    row = await db.get(WhatsAppReply, message_id)
    if row is None or row.ticket_id != ticket_id or not row.inbound_media:
        raise HTTPException(404, "Attachment not found")
    from app.services.whatsapp_media import download_attachment
    return await download_attachment(row.inbound_media)
