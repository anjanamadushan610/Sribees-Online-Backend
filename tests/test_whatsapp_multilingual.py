"""Language parity, stateful navigation, ownership and attachment regressions."""

import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from app.models.order import OrderStatus, PaymentStatus
from app.models.support_ticket import SupportTicket, TicketStatus
from app.models.whatsapp import WhatsAppConversation, WhatsAppReply
from app.services import whatsapp_support as support
from app.services.whatsapp_content import (
    COPY,
    LANGUAGES,
    MENUS,
    language_menu,
    support_menu,
)
from app.services.whatsapp_service import media_references, receive_messages, safe_text
from tests.test_whatsapp_support import MemoryDB, envelope


def conversation(language="en"):
    return WhatsAppConversation(
        phone="94760000000",
        language=language,
        language_selected=True,
        menu="main",
        context={},
        staff_active=False,
        last_message_at=datetime.now(timezone.utc),
    )


def reply(media=None):
    return WhatsAppReply(message_id="wamid." + uuid.uuid4().hex, inbound_media=media)


@pytest.mark.parametrize("language", LANGUAGES)
def test_all_menus_fit_meta_limits_and_language_buttons_are_ordered(language):
    initial = language_menu(language)["interactive"]
    assert [b["reply"]["id"] for b in initial["action"]["buttons"]] == [
        "lang_en",
        "lang_si",
        "lang_ta",
    ]
    assert len(initial["body"]["text"]) <= 1024
    for name in MENUS:
        message = support_menu(name, language)["interactive"]
        assert len(message["body"]["text"]) <= 1024
        assert len(message["action"]["button"]) <= 20
        rows = message["action"]["sections"][0]["rows"]
        assert 1 <= len(rows) <= 10
        assert len({r["id"] for r in rows}) == len(rows)
        assert all(
            1 <= len(r["title"]) <= 24 and len(r.get("description", "")) <= 72
            for r in rows
        )
    assert all(len(values) == 3 and all(values) for values in COPY.values())


def test_every_real_status_has_three_translations():
    assert set(support.ORDER_STATES) == {s.value for s in OrderStatus}
    assert set(support.PAYMENT_STATES) == {s.value for s in PaymentStatus}
    assert set(support.TICKET_STATES) == {s.value for s in TicketStatus}
    assert all(len(v) == 3 for v in support.ORDER_STATES.values())


@pytest.mark.parametrize(
    "phone",
    ["0760000000", "+94 76 000 0000", "94760000000", "0094760000000", "760000000"],
)
def test_phone_forms_use_exact_canonical_identity(phone):
    assert support.normalize_phone(phone) == "94760000000"


@pytest.mark.parametrize("language", LANGUAGES)
async def test_language_selection_persists_and_navigation_parity(language):
    db = MemoryDB()
    await receive_messages(db, envelope("hello"), "123")
    first = next(iter(db.messages.values())).payload
    assert first["interactive"]["action"]["buttons"][0]["reply"]["title"] == "English"
    await receive_messages(db, envelope("lang_" + language, message_id="select"), "123")
    current = db.conversations["94760000000"]
    assert current.language == language and current.language_selected
    for key in MENUS:
        current.menu = key
        current.stage = None
        result = await support.handle_message(db, current, reply(), "language", "text")
        assert result["interactive"]["type"] == "button"
        result = await support.handle_message(db, current, reply(), "MENU", "text")
        assert current.menu == "main" and current.language == language
    current.menu = "orders"
    result = await support.handle_message(db, current, reply(), "1", "text")
    assert current.stage == "order"
    await support.handle_message(db, current, reply(), "cancel", "text")
    assert current.stage is None
    await support.handle_message(db, current, reply(), "lang_ta", "text")
    assert current.language == "ta"


@pytest.mark.parametrize("language", LANGUAGES)
@pytest.mark.parametrize(
    "issue",
    sorted(
        support.DELIVERY_ISSUES
        | support.ITEM_ISSUES
        | support.PAYMENT_ISSUES
        | {"order_other"}
    ),
)
async def test_inquiry_stages_create_one_ticket(language, issue, monkeypatch):
    db = MemoryDB()
    current = conversation(language)
    order = SimpleNamespace(
        order_number="FC-20261111-TEST01",
        order_id=uuid.uuid4(),
        branch_id=None,
        status="confirmed",
        payment_status="paid",
        delivery_address_id=None,
        delivery_slot_date=None,
        refund_amount=None,
    )
    monkeypatch.setattr(support, "owned_order", AsyncMock(return_value=order))
    monkeypatch.setattr(support, "customer_for_phone", AsyncMock(return_value=None))
    if issue == "confirmation":
        monkeypatch.setattr(
            support, "order_details", AsyncMock(return_value="Verified status")
        )
    current.menu = (
        "delivery"
        if issue in support.DELIVERY_ISSUES
        else "items"
        if issue in support.ITEM_ISSUES
        else "payments"
        if issue in support.PAYMENT_ISSUES
        else "orders"
    )
    await support.handle_message(db, current, reply(), issue, "interactive")
    result = await support.handle_message(
        db, current, reply(), order.order_number, "text"
    )
    if issue == "confirmation":
        assert "Verified status" in result["text"]["body"]
        return
    if issue in support.ITEM_ISSUES:
        await support.handle_message(db, current, reply(), "Milk", "text")
        await support.handle_message(db, current, reply(), "2", "text")
    if issue in support.PAYMENT_ISSUES:
        await support.handle_message(db, current, reply(), "card", "text")
        await support.handle_message(db, current, reply(), "REF-TEST-1", "text")
    result = await support.handle_message(
        db, current, reply(), "Please investigate this problem", "text"
    )
    if issue in support.ITEM_ISSUES | support.PAYMENT_ISSUES:
        attachment = reply({"id": "123456", "mime_type": "image/jpeg"})
        result = await support.handle_message(db, current, attachment, "", "image")
        assert attachment.ticket_id == current.ticket_id
    assert len(db.tickets) == 1
    ticket = next(iter(db.tickets.values()))
    assert (
        ticket.preferred_language == language
        and ticket.related_order_id == order.order_id
    )
    assert ticket.ticket_number in result["text"]["body"]
    await support.handle_message(db, current, reply(), "Another message", "text")
    assert len(db.tickets) == 1


async def test_wrong_customer_never_reaches_order_lookup():
    db = SimpleNamespace(
        execute=AsyncMock(
            return_value=SimpleNamespace(
                scalars=lambda: SimpleNamespace(all=lambda: [])
            )
        ),
        scalar=AsyncMock(),
    )
    assert await support.owned_order(db, "94760000000", "FC-20261111-TEST01") is None
    db.scalar.assert_not_called()


async def test_ambiguous_normalized_accounts_are_not_trusted():
    users = [SimpleNamespace(phone="+94760000000"), SimpleNamespace(phone="0760000000")]
    db = SimpleNamespace(
        execute=AsyncMock(
            return_value=SimpleNamespace(
                scalars=lambda: SimpleNamespace(all=lambda: users)
            )
        )
    )
    assert await support.customer_for_phone(db, "94760000000") is None


async def test_ticket_lookup_checks_exact_phone_and_reference():
    ticket = SupportTicket(
        ticket_number="TKT-20261111-ABCDE", contact_phone="+94761111111"
    )
    db = SimpleNamespace(scalar=AsyncMock(return_value=ticket))
    assert await support.owned_ticket(db, "94760000000", ticket.ticket_number) is None
    db.scalar.reset_mock()
    assert await support.owned_ticket(db, "94760000000", "%' OR 1=1") is None
    db.scalar.assert_not_called()


async def test_no_delivery_estimate_is_invented():
    order = SimpleNamespace(
        order_number="FC-TEST",
        status="confirmed",
        payment_status="pending",
        delivery_address_id=None,
        delivery_slot_date=None,
        refund_amount=None,
    )
    text = await support.order_details(MemoryDB(), order, "en")
    assert "Estimated" not in text and "Delivery slot" not in text
    assert "Order confirmed" in text


async def test_attachment_endpoint_checks_ticket_scope(monkeypatch):
    from app.api.v1 import admin_whatsapp

    download = AsyncMock()
    monkeypatch.setattr(admin_whatsapp, "download_attachment", download, raising=False)
    monkeypatch.setattr(
        admin_whatsapp, "ticket_context", AsyncMock(return_value=(None, None))
    )
    db = SimpleNamespace(
        get=AsyncMock(
            return_value=WhatsAppReply(
                ticket_id=uuid.uuid4(), inbound_media={"id": "123"}
            )
        )
    )
    with pytest.raises(HTTPException) as error:
        await admin_whatsapp.attachment(uuid.uuid4(), uuid.uuid4(), db, object())
    assert error.value.status_code == 404
    download.assert_not_called()


def test_secrets_are_redacted_before_retention():
    assert "123456" not in safe_text("My OTP: 123456")
    assert "123456" not in safe_text("123456")
    assert "4111" not in safe_text("card 4111 1111 1111 1111")
    assert safe_text("FC-20261111-ABCDEF") == "FC-20261111-ABCDEF"


def test_media_only_records_allowed_references_for_our_number():
    event = envelope(kind="image")
    message = event["entry"][0]["changes"][0]["value"]["messages"][0]
    message["image"] = {"id": "12345", "mime_type": "image/jpeg"}
    assert media_references(event, "123")[message["id"]] == {
        "id": "12345",
        "mime_type": "image/jpeg",
    }
    assert not media_references(event, "other")
    message["image"]["mime_type"] = "text/html"
    assert not media_references(event, "123")


@pytest.mark.parametrize("language", LANGUAGES)
async def test_unbooked_cancellation_requires_confirmation_and_reuses_service(
    language, monkeypatch
):
    from app.services.order_service import OrderService

    db = SimpleNamespace(scalar=AsyncMock())
    current = conversation(language)
    current.stage = "cancel_confirm"
    current.context = {
        "issue": "cancel_order",
        "order": "FC-20261111-TEST01",
        "description": "Ordered by mistake",
    }
    order = SimpleNamespace(
        order_id=uuid.uuid4(),
        order_number=current.context["order"],
        status="confirmed",
        courier_shipment_id=None,
    )
    monkeypatch.setattr(support, "owned_order", AsyncMock(return_value=order))
    cancel = AsyncMock()
    monkeypatch.setattr(OrderService, "cancel_order", cancel)
    db.scalar.return_value = order
    await support.handle_message(db, current, reply(), "anything", "text")
    cancel.assert_not_called()
    result = await support.handle_message(
        db, current, reply(), "confirm_cancel", "interactive"
    )
    cancel.assert_awaited_once()
    assert cancel.call_args.kwargs["commit"] is False
    assert current.stage is None and order.order_number in result["text"]["body"]


async def test_changed_cancellation_state_does_not_cancel(monkeypatch):
    from app.services.order_service import OrderService

    current = conversation()
    current.stage = "cancel_confirm"
    current.context = {"order": "FC-20261111-TEST01"}
    order = SimpleNamespace(
        order_id=uuid.uuid4(), status="delivered", courier_shipment_id="already-booked"
    )
    monkeypatch.setattr(support, "owned_order", AsyncMock(return_value=order))
    cancel = AsyncMock()
    monkeypatch.setattr(OrderService, "cancel_order", cancel)
    db = SimpleNamespace(scalar=AsyncMock(return_value=order))
    result = await support.handle_message(
        db, current, reply(), "confirm_cancel", "interactive"
    )
    assert "cannot be cancelled" in result["text"]["body"]
    cancel.assert_not_called()


async def test_customer_flood_does_not_amplify_replies():
    db = MemoryDB()
    db.scalar = AsyncMock(return_value=31)
    await receive_messages(db, envelope(), "123")
    row = next(iter(db.messages.values()))
    assert row.payload is None and row.status == "ignored"


@pytest.mark.parametrize("language", LANGUAGES)
async def test_human_request_creates_one_ticket_immediately(language, monkeypatch):
    current = conversation(language)
    current.menu = "support"
    db = MemoryDB()
    monkeypatch.setattr(support, "customer_for_phone", AsyncMock(return_value=None))
    result = await support.handle_message(db, current, reply(), "agent", "interactive")
    assert len(db.tickets) == 1
    assert next(iter(db.tickets.values())).ticket_number in result["text"]["body"]
    assert (
        await support.handle_message(db, current, reply(), "More information", "text")
        is None
    )
    assert len(db.tickets) == 1


async def test_media_provider_failure_is_generic(monkeypatch):
    import httpx

    from app.services.whatsapp_media import download_attachment

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(404))
    )
    monkeypatch.setattr(
        "app.services.whatsapp_media.httpx.AsyncClient", lambda **kwargs: client
    )
    with pytest.raises(HTTPException) as error:
        await download_attachment({"id": "123", "mime_type": "image/jpeg"})
    assert error.value.status_code == 502
    assert "token" not in error.value.detail.lower()
