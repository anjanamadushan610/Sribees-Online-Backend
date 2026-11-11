"""One staff email per support ticket, sent outside the webhook transaction."""

from html import escape

from sqlalchemy import select

from app.config.database import async_session_maker
from app.models.support_ticket import SupportTicket
from app.services import email_sender


async def notify_next_ticket():
    if not email_sender.is_configured():
        return False
    async with async_session_maker() as db:
        ticket = await db.scalar(
            select(SupportTicket)
            .where(
                SupportTicket.notification_status == "pending",
            )
            .order_by(SupportTicket.created_at)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if ticket is None:
            return False
        ticket.notification_status = "sending"
        reference = ticket.ticket_number
        ticket_id = ticket.ticket_id
        # Keep volunteered customer text out of email. Staff can read the
        # authorized dashboard for details, attachments and customer identity.
        body = f"New WhatsApp support request {reference}\nCategory: {ticket.category}\nLanguage: {ticket.preferred_language}\nOpen Support Tickets in the Sribees Admin dashboard."
        await db.commit()
    status = "sent"
    try:
        await email_sender.send_email(
            "support@sribeesonline.com",
            f"WhatsApp support {reference}",
            body,
            "<pre>" + escape(body) + "</pre>",
        )
    except email_sender.EmailSendError:
        # An ambiguous SMTP response must not produce repeated staff emails.
        status = "failed"
    async with async_session_maker() as db:
        ticket = await db.get(SupportTicket, ticket_id)
        if ticket:
            ticket.notification_status = status
            await db.commit()
    return True


async def queue_ticket_status(db, ticket):
    """Queue a localized status update in the same transaction as staff changes."""
    from datetime import datetime, timedelta, timezone

    from app.models.whatsapp import WhatsAppConversation, WhatsAppReply
    from app.services.whatsapp_content import copy
    from app.services.whatsapp_menu import text_reply
    from app.services.whatsapp_support import TICKET_STATES, labelled, reset

    conversation = await db.scalar(
        select(WhatsAppConversation)
        .where(WhatsAppConversation.ticket_id == ticket.ticket_id)
        .with_for_update()
    )
    if conversation is None:
        return
    language = conversation.language
    expires = conversation.last_message_at + timedelta(seconds=86100)
    if expires > datetime.now(timezone.utc):
        from app.services.whatsapp_content import translated

        body = (
            labelled("ticket", ticket.ticket_number, language)
            + "\n\n"
            + labelled(
                "ticket_status",
                translated(TICKET_STATES[ticket.status], language),
                language,
            )
        )
        db.add(
            WhatsAppReply(
                message_id=f"ticket-status:{ticket.ticket_id}:{ticket.status}:{ticket.updated_at.isoformat()}",
                phone=conversation.phone,
                ticket_id=ticket.ticket_id,
                payload=text_reply(body + "\n\n" + copy("nav", language)),
                status="pending",
                expires_at=expires,
            )
        )
    if ticket.status in {"resolved", "closed"}:
        conversation.staff_active = False
        conversation.ticket_id = None
        reset(conversation)
