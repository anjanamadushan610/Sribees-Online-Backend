"""Commit tickets and replies together before acknowledging Meta's webhook."""

from datetime import datetime, timedelta, timezone

from sqlalchemy import text as sql_text
from sqlalchemy.dialects.postgresql import insert

from app.api.v1.support import _generate_ticket_number
from app.models.support_ticket import SupportTicket, TicketStatus
from app.models.whatsapp import WhatsAppConversation, WhatsAppReply
from app.services.whatsapp_menu import answer, incoming_messages, menu, text_reply


async def receive_messages(db, payload: dict, phone_number_id: str) -> None:
    now = datetime.now(timezone.utc)
    # Serialize menu state transitions across concurrent webhook deliveries.
    # This lock is released at transaction end, and never covers an HTTP send.
    await db.execute(sql_text("SELECT pg_advisory_xact_lock(8767000836)"))
    for message_id, phone, received_at, body, kind in incoming_messages(
        payload, phone_number_id, now
    ):
        claimed = (
            await db.execute(
                insert(WhatsAppReply)
                .values(
                    message_id=message_id,
                    phone=phone,
                    status="ignored",
                    expires_at=received_at + timedelta(seconds=86100),
                )
                .on_conflict_do_nothing(index_elements=["message_id"])
                .returning(WhatsAppReply.reply_id)
            )
        ).scalar_one_or_none()
        if claimed is None:
            continue
        reply = await db.get(WhatsAppReply, claimed)
        conversation = await db.get(WhatsAppConversation, phone)
        if conversation is None:
            conversation = WhatsAppConversation(
                phone=phone,
                language="en",
                last_message_at=received_at,
                awaiting_details=False,
            )
            db.add(conversation)
        elif (received_at - conversation.last_message_at).total_seconds() >= 86400:
            conversation.awaiting_details = False
            conversation.ticket_id = None
        conversation.last_message_at = max(conversation.last_message_at, received_at)
        command = body.casefold()
        if command in {"menu", "start", "hi", "hello", "si", "en", "සිංහල", "english"}:
            if command in {"si", "සිංහල"}:
                conversation.language = "si"
            elif command in {"en", "english"}:
                conversation.language = "en"
            conversation.awaiting_details = False
            conversation.ticket_id = None
            reply.payload = menu(conversation.language)
        elif conversation.ticket_id:
            ticket = await db.get(SupportTicket, conversation.ticket_id)
            if ticket and ticket.status in {
                TicketStatus.PENDING.value,
                TicketStatus.IN_PROGRESS.value,
            }:
                addition = (
                    body
                    or "[Customer sent a non-text message; request details by phone.]"
                )
                # Bound ticket size even if a sender floods the conversation.
                ticket.message = (
                    ticket.message + "\n\nWhatsApp follow-up: " + addition
                )[-16000:]
            else:
                conversation.ticket_id = None
                reply.payload = menu(conversation.language)
        elif command == "support":
            conversation.awaiting_details = True
            reply.payload = text_reply(
                "ඔබේ ගැටලුව text message එකකින් එවන්න. අවශ්‍ය නම් order number එකත් එක් කරන්න. Password, OTP හෝ card විස්තර එවන්න එපා."
                if conversation.language == "si"
                else "Please describe the issue in a text message. Include your order number if relevant. Do not send passwords, OTPs or card details."
            )
        elif conversation.awaiting_details:
            if kind != "text" or len(body) < 5:
                reply.payload = text_reply(
                    "Please describe the issue in text (at least 5 characters). / ගැටලුව text ලෙස එවන්න."
                )
            else:
                ticket = SupportTicket(
                    ticket_number=_generate_ticket_number(),
                    contact_phone="+" + phone,
                    category="general",
                    message="WhatsApp support request:\n" + body,
                    status=TicketStatus.PENDING.value,
                )
                db.add(ticket)
                await db.flush()
                conversation.ticket_id = ticket.ticket_id
                conversation.awaiting_details = False
                reply.payload = text_reply(
                    (
                        "ඔබගේ support ඉල්ලීම ලැබුණා. යොමු අංකය "
                        if conversation.language == "si"
                        else "Your support request has been received. Reference "
                    )
                    + ticket.ticket_number
                    + "\nSupport: support@sribeesonline.com"
                )
        elif command in {"delivery", "app"}:
            reply.payload = answer(command, conversation.language)
        else:
            if any("\u0d80" <= character <= "\u0dff" for character in body):
                conversation.language = "si"
            reply.payload = menu(conversation.language)
        if reply.payload:
            reply.status = "pending"
        # autoflush is disabled in this application. A second message from the
        # same sender in this batch must find the newly created conversation.
        await db.flush()
    await db.commit()
