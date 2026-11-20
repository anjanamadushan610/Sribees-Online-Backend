"""Commit incoming messages, tickets and queued replies atomically."""

import re
from datetime import datetime, timedelta, timezone

from loguru import logger
from sqlalchemy import func, select
from sqlalchemy import text as sql_text
from sqlalchemy.dialects.postgresql import insert

from app.models.whatsapp import WhatsAppConversation, WhatsAppReply
from app.services.whatsapp_content import text_message
from app.services.whatsapp_menu import incoming_messages
from app.services.whatsapp_receipts import receive_statuses
from app.services.whatsapp_support import handle_message, reset


def safe_text(body):
    # Retain the issue without retaining credentials volunteered by a customer.
    body = re.sub(
        r"(?i)\b(password|passwd|otp|cvv|cvc|pin)\s*[:=]?\s*\S+", r"\1 [REDACTED]", body
    )
    body = re.sub(r"(?<!\d)(?:\d[ -]?){13,19}(?!\d)", "[REDACTED]", body)
    if re.fullmatch(r"\s*\d{4,8}\s*", body):
        return "[REDACTED]"
    return body


def media_references(payload, phone_number_id):
    refs = {}
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            if value.get("metadata", {}).get("phone_number_id") != phone_number_id:
                continue
            for message in value.get("messages", []):
                kind = message.get("type")
                media = message.get(kind, {}) if kind in {"image", "document"} else {}
                media_id = media.get("id", "")
                mime = media.get("mime_type", "")
                if (
                    isinstance(media_id, str)
                    and media_id.isascii()
                    and media_id.isdigit()
                    and len(media_id) <= 100
                    and mime in {"image/jpeg", "image/png", "application/pdf"}
                ):
                    refs[message.get("id")] = {"id": media_id, "mime_type": mime}
    return refs


async def receive_messages(db, payload: dict, phone_number_id: str) -> None:
    now = datetime.now(timezone.utc)
    await db.execute(sql_text("SELECT pg_advisory_xact_lock(8767000836)"))
    await receive_statuses(db, payload, phone_number_id)
    media = media_references(payload, phone_number_id)
    for message_id, phone, received_at, body, kind in incoming_messages(
        payload, phone_number_id, now
    ):
        body = safe_text(body)
        claimed = (
            await db.execute(
                insert(WhatsAppReply)
                .values(
                    message_id=message_id,
                    phone=phone,
                    inbound_text=body,
                    inbound_type=kind,
                    inbound_media=media.get(message_id),
                    received_at=received_at,
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
                language_selected=False,
                last_message_at=received_at,
                awaiting_details=False,
                staff_active=False,
                menu="main",
                context={},
            )
            db.add(conversation)
            await db.flush()
        elif (
            received_at - conversation.last_message_at
        ).total_seconds() >= 86400 and not conversation.staff_active:
            # Language persists. An unfinished request expires after a day.
            reset(conversation)
        conversation.last_message_at = max(conversation.last_message_at, received_at)
        recent_count = await db.scalar(
            select(func.count())
            .select_from(WhatsAppReply)
            .where(
                WhatsAppReply.phone == phone,
                WhatsAppReply.received_at >= now - timedelta(minutes=1),
            )
        )
        if (recent_count or 0) > 30:
            # Retain the signed inbound event but do not amplify a flood with replies.
            continue
        try:
            async with db.begin_nested():
                reply.payload = await handle_message(
                    db, conversation, reply, body, kind
                )
        except Exception:
            # Savepoint rolls back partial inquiries but retains the inbound claim.
            # Restore expired ORM attributes before using the localized fallback.
            await db.refresh(conversation)
            await db.refresh(reply)
            reply.payload = text_message("error", conversation.language)
            logger.error("WhatsApp support flow failed; inbound message retained")
        if reply.payload:
            reply.status = "pending"
        await db.flush()
    await db.commit()
