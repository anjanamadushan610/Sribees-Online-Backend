"""Apply signed Meta delivery receipts without regressing delivery state."""

from uuid import UUID

from sqlalchemy import or_, select

from app.models.whatsapp import WhatsAppReply

DELIVERY_RANK = {"accepted": 0, "sent": 1, "delivered": 2, "read": 3}


def advances_status(current: str, incoming: str) -> bool:
    if incoming == "failed":
        return current not in {"delivered", "read", "expired", "cancelled", "ignored"}
    if incoming not in DELIVERY_RANK or current in {"expired", "cancelled", "ignored"}:
        return False
    return DELIVERY_RANK[incoming] > DELIVERY_RANK.get(current, -1)


async def receive_statuses(db, payload: dict, phone_number_id: str) -> None:
    if payload.get("object") != "whatsapp_business_account":
        return
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            if (
                change.get("field") != "messages"
                or value.get("metadata", {}).get("phone_number_id") != phone_number_id
            ):
                continue
            for receipt in value.get("statuses", []):
                provider_id = receipt.get("id")
                if not isinstance(provider_id, str) or not 1 <= len(provider_id) <= 255:
                    continue
                conditions = [WhatsAppReply.provider_message_id == provider_id]
                try:
                    callback_id = UUID(receipt.get("biz_opaque_callback_data", ""))
                    conditions.append(WhatsAppReply.reply_id == callback_id)
                except (ValueError, TypeError, AttributeError):
                    pass
                job = (
                    await db.execute(
                        select(WhatsAppReply).where(or_(*conditions)).with_for_update()
                    )
                ).scalar_one_or_none()
                if job is None or job.phone != receipt.get("recipient_id"):
                    continue
                if advances_status(job.status, receipt.get("status")):
                    job.status = receipt["status"]
                    job.provider_message_id = provider_id
                    errors = receipt.get("errors") or []
                    code = str(errors[0].get("code", "")) if errors else ""
                    job.error_code = (
                        code
                        if code.isascii() and code.isdigit() and len(code) <= 30
                        else None
                    )
                    await db.flush()
