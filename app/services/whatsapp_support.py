"""Stateful support flows using existing customers, orders, wallets and tickets."""

import re

from sqlalchemy import func, select, update
from sqlalchemy.orm import selectinload

from app.api.v1.support import _generate_ticket_number
from app.models.order import Order
from app.models.support_ticket import SupportTicket, TicketStatus
from app.models.user import Address, User
from app.models.wallet import Wallet
from app.models.whatsapp import WhatsAppReply
from app.services.whatsapp_content import (
    MENUS,
    copy,
    language_menu,
    support_menu,
    text_message,
    translated,
)
from app.services.whatsapp_menu import text_reply

ORDER_STATES = {
    "awaiting_payment": (
        "Awaiting payment",
        "ගෙවීම බලාපොරොත්තුවෙන්",
        "கட்டணத்திற்காக காத்திருக்கிறது",
    ),
    "pending": ("Order pending", "ඇණවුම බලාපොරොත්තුවෙන්", "ஆர்டர் நிலுவையில்"),
    "confirmed": ("Order confirmed", "ඇණවුම තහවුරුයි", "ஆர்டர் உறுதி செய்யப்பட்டது"),
    "processing": ("Preparing your order", "ඇණවුම සූදානම් කරමින්", "ஆர்டர் தயாராகிறது"),
    "packing": (
        "Packing your order",
        "ඇණවුම ඇසුරුම් කරමින්",
        "ஆர்டர் பொதியிடப்படுகிறது",
    ),
    "packed": ("Order packed", "ඇණවුම ඇසුරුම් කර ඇත", "ஆர்டர் பொதியிடப்பட்டது"),
    "ready_for_pickup": (
        "Ready for courier pickup",
        "බෙදාහැරීම සඳහා රැගෙන යාමට සූදානම්",
        "கூரியர் சேகரிப்புக்கு தயார்",
    ),
    "handed_to_courier": (
        "Handed to courier",
        "බෙදාහැරීමේ සේවයට භාර දී ඇත",
        "கூரியரிடம் ஒப்படைக்கப்பட்டது",
    ),
    "shipped": ("In transit", "ප්‍රවාහනය කරමින්", "போக்குவரத்தில் உள்ளது"),
    "out_for_delivery": (
        "Out for delivery",
        "බෙදාහැරීමට පැමිණෙමින්",
        "விநியோகத்திற்கு வருகிறது",
    ),
    "delivered": ("Delivered", "බෙදාහැර ඇත", "விநியோகிக்கப்பட்டது"),
    "delivery_failed": (
        "Delivery unsuccessful",
        "බෙදාහැරීම අසාර්ථකයි",
        "விநியோகம் தோல்வி",
    ),
    "rto_initiated": (
        "Returning to store",
        "වෙළඳසැලට ආපසු යවමින්",
        "கடைக்குத் திருப்பப்படுகிறது",
    ),
    "cancelled": ("Cancelled", "අවලංගුයි", "ரத்து செய்யப்பட்டது"),
    "return_requested": (
        "Return requested",
        "ආපසු භාරගැනීම ඉල්ලා ඇත",
        "திருப்ப கோரப்பட்டது",
    ),
    "return_approved": (
        "Return approved",
        "ආපසු භාරගැනීම අනුමතයි",
        "திருப்ப அனுமதிக்கப்பட்டது",
    ),
    "return_rejected": (
        "Return declined",
        "ආපසු භාරගැනීම ප්‍රතික්ෂේපයි",
        "திருப்ப மறுக்கப்பட்டது",
    ),
    "refunded": ("Refund processed", "මුදල් ආපසු බැර කර ඇත", "பணம் திருப்பப்பட்டது"),
}
PAYMENT_STATES = {
    "pending": ("Pending", "බලාපොරොත්තුවෙන්", "நிலுவையில்"),
    "paid": ("Paid", "ගෙවා ඇත", "செலுத்தப்பட்டது"),
    "failed": ("Failed", "අසාර්ථකයි", "தோல்வி"),
    "refunded": ("Refunded", "ආපසු බැර කර ඇත", "திருப்பப்பட்டது"),
}
TICKET_STATES = {
    "pending": ("Pending", "බලාපොරොත්තුවෙන්", "நிலுவையில்"),
    "in_progress": ("In progress", "පරීක්ෂා කරමින්", "செயல்பாட்டில்"),
    "resolved": ("Resolved", "විසඳා ඇත", "தீர்க்கப்பட்டது"),
    "closed": ("Closed", "අවසන් කර ඇත", "முடிக்கப்பட்டது"),
}
DELIVERY_ISSUES = {"delayed", "not_received", "contact", "address", "delivery_other"}
ITEM_ISSUES = {"missing", "wrong", "damaged", "quality", "expired"}
PAYMENT_ISSUES = {
    "payment_failed",
    "deducted",
    "bank",
    "refund",
    "confirmation",
    "payment_other",
}


def normalize_phone(value):
    digits = re.sub(r"[^0-9]", "", value or "")
    if digits.startswith("00"):
        digits = digits[2:]
    from app.utils.identity import normalize_mobile

    try:
        return normalize_mobile(digits).lstrip("+")
    except ValueError:
        pass
    return digits if 7 <= len(digits) <= 15 else None


async def customer_for_phone(db, phone):
    canonical = normalize_phone(phone)
    if not canonical:
        return None
    # Exact canonical equality, never suffix matching. Legacy local +94 formats
    # are normalized in SQL, but every candidate must match again in Python.
    digits = func.regexp_replace(User.phone, "[^0-9]", "", "g")
    candidates = {canonical, "00" + canonical}
    if canonical.startswith("94") and len(canonical) == 11:
        candidates.update({"0" + canonical[2:], canonical[2:]})
    users = (
        (
            await db.execute(
                select(User)
                .where(
                    digits.in_(candidates),
                    User.deleted_at.is_(None),
                    User.is_active.is_(True),
                    User.is_blocked.is_(False),
                    User.is_phone_verified.is_(True),
                )
                .limit(2)
            )
        )
        .scalars()
        .all()
    )
    matches = [user for user in users if normalize_phone(user.phone) == canonical]
    return matches[0] if len(matches) == 1 else None


async def owned_order(db, phone, number):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{3,49}", number.strip()):
        return None
    customer = await customer_for_phone(db, phone)
    if customer is None:
        return None
    return await db.scalar(
        select(Order)
        .options(selectinload(Order.items))
        .where(
            Order.order_number == number.strip().upper(),
            Order.user_id == customer.user_id,
        )
    )


async def owned_ticket(db, phone, number):
    if not re.fullmatch(r"TKT-[0-9]{8}-[A-Za-z0-9]{5,8}", number.strip(), re.I):
        return None
    ticket = await db.scalar(
        select(SupportTicket).where(
            SupportTicket.ticket_number == number.strip().upper(),
            func.regexp_replace(SupportTicket.contact_phone, "[^0-9]", "", "g")
            == normalize_phone(phone),
        )
    )
    return (
        ticket
        if ticket and normalize_phone(ticket.contact_phone) == normalize_phone(phone)
        else None
    )


def reset(conversation, menu="main"):
    conversation.menu = menu
    conversation.stage = None
    conversation.context = {}
    conversation.awaiting_details = False


def prompt(conversation, stage, key):
    conversation.stage = stage
    return text_message(key, conversation.language)


def labelled(key, value, language):
    labels = {
        "order": ("Order Number", "ඇණවුම් අංකය", "ஆர்டர் எண்"),
        "status": ("Order Status", "ඇණවුමේ තත්ත්වය", "ஆர்டரின் நிலை"),
        "payment": ("Payment Status", "ගෙවීමේ තත්ත්වය", "கட்டண நிலை"),
        "address": ("Delivery Address", "බෙදාහැරීමේ ලිපිනය", "விநியோக முகவரி"),
        "delivery": ("Delivery slot", "බෙදාහැරීමේ කාලය", "விநியோக நேரம்"),
        "refund": (
            "Recorded refund amount (LKR)",
            "සටහන් කළ ආපසු බැර මුදල (LKR)",
            "பதிவான பணமீட்பு தொகை (LKR)",
        ),
        "ticket": ("Ticket Number", "සහාය යොමු අංකය", "கோரிக்கை எண்"),
        "ticket_status": ("Ticket Status", "සහාය ඉල්ලීමේ තත්ත්වය", "கோரிக்கையின் நிலை"),
        "updated": ("Last updated", "අවසන් යාවත්කාලීනය", "கடைசிப் புதுப்பிப்பு"),
    }
    return translated(labels[key], language) + ": " + str(value)


async def order_details(db, order, language):
    unknown = (
        "Please contact support for this status",
        "මෙම තත්ත්වය ගැන සහාය සේවය අමතන්න",
        "இந்த நிலைக்காக ஆதரவை அணுகவும்",
    )
    lines = [
        labelled("order", order.order_number, language),
        labelled(
            "status",
            translated(ORDER_STATES.get(order.status, unknown), language),
            language,
        ),
        labelled(
            "payment",
            translated(PAYMENT_STATES.get(order.payment_status, unknown), language),
            language,
        ),
    ]
    if order.delivery_address_id:
        address = await db.get(Address, order.delivery_address_id)
        if address and address.user_id == order.user_id:
            lines.append(
                labelled(
                    "address",
                    ", ".join(
                        filter(
                            None,
                            [
                                address.address_line1,
                                address.address_line2,
                                address.postal_city,
                            ],
                        )
                    ),
                    language,
                )
            )
    if order.delivery_slot_date:
        slot = order.delivery_slot_date.strftime("%Y-%m-%d")
        if order.delivery_slot_time:
            slot += " " + order.delivery_slot_time
        lines.append(labelled("delivery", slot, language))
    if order.refund_amount is not None:
        lines.append(labelled("refund", f"{order.refund_amount:.2f}", language))
    return "\n\n".join(lines)


async def create_ticket(db, conversation, reply, description):
    context = dict(conversation.context or {})
    order = (
        await owned_order(db, conversation.phone, context.get("order", ""))
        if context.get("order")
        else None
    )
    customer = await customer_for_phone(db, conversation.phone)
    ticket = SupportTicket(
        ticket_number=_generate_ticket_number(),
        user_id=customer.user_id if customer else None,
        customer_name=customer.full_name if customer else None,
        branch_id=order.branch_id if order else None,
        contact_phone="+" + conversation.phone,
        category=context.get("category", "general"),
        message="\n".join(
            ["WhatsApp support request", "Language: " + conversation.language]
            + [
                f"{key}: {value}"
                for key, value in context.items()
                if key != "category" and not key.startswith("_")
            ]
            + [description]
        ),
        status=TicketStatus.PENDING.value,
        preferred_language=conversation.language,
        related_order_id=order.order_id if order else None,
        notification_status="pending",
    )
    db.add(ticket)
    await db.flush()
    message_ids = context.get("_message_ids", [])
    if message_ids:
        await db.execute(
            update(WhatsAppReply)
            .where(
                WhatsAppReply.phone == conversation.phone,
                WhatsAppReply.message_id.in_(message_ids),
                WhatsAppReply.ticket_id.is_(None),
            )
            .values(ticket_id=ticket.ticket_id)
        )
    reply.ticket_id = ticket.ticket_id
    conversation.ticket_id = ticket.ticket_id
    reset(conversation)
    return text_message(
        "ticket_created", conversation.language, number=ticket.ticket_number
    )


async def handle_message(db, conversation, reply, body, kind):
    language = conversation.language
    command = body.strip().casefold()
    # Active handover is released by staff, never by arbitrary customer text.
    if conversation.ticket_id:
        ticket = await db.get(SupportTicket, conversation.ticket_id)
        if ticket and ticket.status in {"pending", "in_progress"}:
            reply.ticket_id = ticket.ticket_id
            if conversation.staff_active or command not in {
                "menu",
                "main",
                "start",
                "language",
                "cancel",
            }:
                if body:
                    ticket.message = (
                        ticket.message + "\n\nWhatsApp follow-up: " + body
                    )[-16000:]
                return None
        conversation.ticket_id = None
        conversation.staff_active = False

    languages = {
        "en": "en",
        "english": "en",
        "si": "si",
        "සිංහල": "si",
        "ta": "ta",
        "தமிழ்": "ta",
        "lang_en": "en",
        "lang_si": "si",
        "lang_ta": "ta",
    }
    if command in languages:
        conversation.language = languages[command]
        conversation.language_selected = True
        reset(conversation)
        return support_menu("main", conversation.language)
    if not conversation.language_selected:
        conversation.stage = "language"
        return language_menu(language)
    if command in {"language", "change language"}:
        return language_menu(language)
    if command in {"menu", "main", "start", "cancel"}:
        reset(conversation)
        return support_menu("main", language)
    if command == "back":
        parent = "orders" if conversation.menu in {"delivery", "items"} else "main"
        reset(conversation, parent)
        return support_menu(parent, language)

    context = dict(conversation.context or {})
    if conversation.stage:
        context["_message_ids"] = (
            context.get("_message_ids", []) + [reply.message_id]
        )[-30:]
        conversation.context = context
    if conversation.stage == "cancel_confirm":
        if command != "confirm_cancel":
            return text_message("invalid", language)
        order = await owned_order(db, conversation.phone, context.get("order", ""))
        if order is None:
            reset(conversation)
            return text_message("order_missing", language)
        # Serialize with order fulfillment and payment confirmation.
        order = await db.scalar(
            select(Order)
            .where(Order.order_id == order.order_id)
            .options(selectinload(Order.items))
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        from app.services.order_service import OrderService

        if OrderService.customer_cancel_state(order) != OrderService.CANCEL_FREE:
            reset(conversation)
            return text_message("cancel_blocked", language)
        await OrderService.cancel_order(
            db,
            order,
            context.get("description"),
            cancelled_by="customer",
            commit=False,
            context={"client_platform": "whatsapp", "message_id": reply.message_id},
        )
        reset(conversation)
        return text_message("cancel_done", language, order=order.order_number)
    if conversation.stage == "ticket":
        ticket = await owned_ticket(db, conversation.phone, body)
        if ticket is None:
            return text_message("ticket_missing", language)
        reset(conversation)
        lines = [
            labelled("ticket", ticket.ticket_number, language),
            labelled(
                "ticket_status",
                translated(TICKET_STATES[ticket.status], language),
                language,
            ),
        ]
        if ticket.updated_at:
            lines.append(
                labelled(
                    "updated",
                    ticket.updated_at.strftime("%Y-%m-%d %H:%M UTC"),
                    language,
                )
            )
        return text_reply("\n\n".join(lines) + "\n\n" + copy("nav", language))
    if conversation.stage == "order":
        if command == "skip" and context["issue"] in PAYMENT_ISSUES - {
            "confirmation",
            "refund",
        }:
            return prompt(conversation, "method", "method_prompt")
        order = await owned_order(db, conversation.phone, body)
        if order is None:
            return text_message("order_missing", language)
        context["order"] = order.order_number
        conversation.context = context
        if context["issue"] in {"track", "confirmation"}:
            result = await order_details(db, order, language)
            reset(conversation)
            return text_reply(result + "\n\n" + copy("nav", language))
        if context["issue"] == "cancel_order":
            from app.services.order_service import OrderService

            if not OrderService.customer_can_cancel(order):
                reset(conversation)
                return text_message("cancel_blocked", language)
            context["cancel_state"] = OrderService.customer_cancel_state(order)
            conversation.context = context
            return prompt(
                conversation,
                "description",
                "details_prompt"
                if context["cancel_state"] == OrderService.CANCEL_FREE
                else "cancel_request",
            )
        if context["issue"] in ITEM_ISSUES:
            return prompt(conversation, "product", "product_prompt")
        if context["issue"] in PAYMENT_ISSUES:
            response = prompt(conversation, "method", "method_prompt")
            if context["issue"] == "refund":
                response["text"]["body"] = (
                    await order_details(db, order, language)
                    + "\n\n"
                    + response["text"]["body"]
                )
            return response
        return prompt(conversation, "description", "details_prompt")
    if conversation.stage in {
        "product",
        "quantity",
        "method",
        "reference",
        "description",
    }:
        stage = conversation.stage
        if kind != "text" or len(body.strip()) < (5 if stage == "description" else 1):
            return text_message("invalid", language)
        if stage == "quantity" and (
            not body.isascii() or not body.isdigit() or not 1 <= int(body) <= 999
        ):
            return text_message("quantity_prompt", language)
        context[stage] = body[:2000]
        conversation.context = context
        if stage == "product":
            return prompt(conversation, "quantity", "quantity_prompt")
        if stage == "quantity" or stage == "reference":
            return prompt(conversation, "description", "details_prompt")
        if stage == "method":
            return prompt(conversation, "reference", "reference_prompt")
        if (
            context.get("issue") == "cancel_order"
            and context.get("cancel_state") == "free"
        ):
            conversation.stage = "cancel_confirm"
            return {
                "type": "interactive",
                "interactive": {
                    "type": "button",
                    "body": {
                        "text": copy("cancel_confirm", language, order=context["order"])
                    },
                    "action": {
                        "buttons": [
                            {
                                "type": "reply",
                                "reply": {
                                    "id": "confirm_cancel",
                                    "title": translated(
                                        (
                                            "Confirm cancellation",
                                            "අවලංගු කරන්න",
                                            "ரத்து உறுதிப்படுத்து",
                                        ),
                                        language,
                                    ),
                                },
                            },
                            {
                                "type": "reply",
                                "reply": {
                                    "id": "cancel",
                                    "title": translated(
                                        (
                                            "Keep order",
                                            "ඇණවුම තබාගන්න",
                                            "ஆர்டரை வைத்திரு",
                                        ),
                                        language,
                                    ),
                                },
                            },
                        ]
                    },
                },
            }
        if context.get("issue") in ITEM_ISSUES | PAYMENT_ISSUES:
            return prompt(conversation, "photo", "photo_prompt")
        return await create_ticket(db, conversation, reply, body)
    if conversation.stage == "photo":
        if command != "skip" and not reply.inbound_media:
            return text_message("photo_prompt", language)
        return await create_ticket(
            db, conversation, reply, context.get("description", "")
        )

    # Numeric shortcuts only refer to the currently displayed menu.
    choices = MENUS.get(conversation.menu, MENUS["main"])
    if command.isascii() and command.isdigit() and 1 <= int(command) <= len(choices):
        command = choices[int(command) - 1]
    allowed = set(choices) | {"menu", "language"}
    if command in {"hi", "hello"}:
        reset(conversation)
        return support_menu("main", language)
    if command not in allowed:
        return text_message("invalid", language)
    if command in MENUS:
        reset(conversation, command)
        return support_menu(command, language)
    if command in {
        "download",
        "registration",
        "otp",
        "password",
        "profile",
        "deletion",
    }:
        return text_message(command, language)
    if command in {"current_offers", "campaigns"}:
        from app.api.v1.coupons import get_available_coupons
        from app.config.redis import get_redis_client

        user = await customer_for_phone(db, conversation.phone)
        available = await get_available_coupons(
            db=db, redis=get_redis_client(), current_user=user
        )
        # The existing presenter applies branch, audience, expiry and usage rules.
        data = available.get("data", [])
        if isinstance(data, dict):
            data = data.get("coupons", [])
        codes = [
            item.get("code")
            for item in data
            if isinstance(item, dict)
            and item.get("code")
            and item.get("usable")
            and item.get("is_first_order_eligible", True)
        ]
        result = copy("offers", language)
        if codes:
            result += "\n\n" + "\n".join(codes[:5])
        return text_reply(result + "\n\n" + copy("nav", language))
    if command in {"wallet", "free_delivery"}:
        user = await customer_for_phone(db, conversation.phone)
        if user is None:
            return text_message("identity", language)
        if command == "wallet":
            balance = await db.scalar(
                select(Wallet.balance).where(Wallet.user_id == user.user_id)
            )
            return text_message("wallet", language, balance=f"{balance or 0:.2f}")
        from app.services.whatsapp_benefits import free_delivery_summary

        summary = await free_delivery_summary(db, user, language)
        return text_reply(summary + "\n\n" + copy("nav", language))
    if command == "agent":
        conversation.context = {
            "issue": command,
            "category": "general",
            "_message_ids": [reply.message_id],
        }
        return await create_ticket(
            db, conversation, reply, copy("agent_request", language)
        )
    if command == "ticket":
        return prompt(conversation, "ticket", "ticket_prompt")
    category = "payment" if command in PAYMENT_ISSUES else "general"
    if command in DELIVERY_ISSUES | ITEM_ISSUES | {
        "track",
        "cancel_order",
        "order_other",
    }:
        category = "order"
    conversation.context = {
        "issue": command,
        "category": category,
        "_message_ids": [reply.message_id],
    }
    if command in DELIVERY_ISSUES | ITEM_ISSUES | PAYMENT_ISSUES | {
        "track",
        "cancel_order",
        "order_other",
    }:
        prompt_key = (
            "optional_order_prompt"
            if command in PAYMENT_ISSUES - {"confirmation", "refund"}
            else "order_prompt"
        )
        return prompt(conversation, "order", prompt_key)
    return prompt(conversation, "description", "details_prompt")
