"""Real PostgreSQL regression tests for webhook deduplication and atomicity."""

import asyncio
import uuid

import pytest
from sqlalchemy import delete, select

from app.models.support_ticket import SupportTicket
from app.models.whatsapp import WhatsAppConversation, WhatsAppReply
from app.services.whatsapp_service import receive_messages
from tests.integration.conftest import requires_disposable_db
from tests.test_whatsapp_support import envelope

pytestmark = requires_disposable_db


@pytest.fixture
async def whatsapp_case(db_session_factory):
    phone = "9476" + str(uuid.uuid4().int)[-7:]
    prefix = "wamid.test." + uuid.uuid4().hex

    def event(body, suffix):
        payload = envelope(body, message_id=prefix + suffix)
        payload["entry"][0]["changes"][0]["value"]["messages"][0]["from"] = phone
        return payload

    yield phone, prefix, event
    async with db_session_factory() as db:
        await db.execute(
            delete(WhatsAppConversation).where(WhatsAppConversation.phone == phone)
        )
        await db.execute(delete(WhatsAppReply).where(WhatsAppReply.phone == phone))
        await db.execute(
            delete(SupportTicket).where(SupportTicket.contact_phone == "+" + phone)
        )
        await db.commit()


async def test_concurrent_duplicates_make_one_ticket(db_session_factory, whatsapp_case):
    phone, prefix, event = whatsapp_case
    async with db_session_factory() as db:
        await receive_messages(db, event("en", "language"), "123")
        await receive_messages(db, event("support", "start"), "123")
        await receive_messages(db, event("2", "inquiry"), "123")

    async def deliver():
        async with db_session_factory() as db:
            await receive_messages(
                db, event("Please help with delivery", "details"), "123"
            )

    await asyncio.gather(deliver(), deliver(), deliver())
    async with db_session_factory() as db:
        tickets = list(
            (
                await db.execute(
                    select(SupportTicket).where(
                        SupportTicket.contact_phone == "+" + phone
                    )
                )
            ).scalars()
        )
        replies = list(
            (
                await db.execute(
                    select(WhatsAppReply).where(
                        WhatsAppReply.message_id == prefix + "details"
                    )
                )
            ).scalars()
        )
        assert len(tickets) == len(replies) == 1
        assert replies[0].status == "pending"


async def test_batched_messages_share_conversation(db_session_factory, whatsapp_case):
    phone, _, event = whatsapp_case
    async with db_session_factory() as db:
        await receive_messages(db, event("en", "language"), "123")
        await receive_messages(db, event("support", "menu"), "123")
    payload = event("2", "start")
    messages = payload["entry"][0]["changes"][0]["value"]["messages"]
    messages.extend(
        event("My order has not arrived", "details")["entry"][0]["changes"][0]["value"][
            "messages"
        ]
    )
    async with db_session_factory() as db:
        await receive_messages(db, payload, "123")
        conversation = await db.get(WhatsAppConversation, phone)
        assert conversation.ticket_id is not None
        assert not conversation.awaiting_details


async def test_failed_commit_can_be_retried(
    db_session_factory, whatsapp_case, monkeypatch
):
    phone, prefix, event = whatsapp_case
    async with db_session_factory() as db:
        await receive_messages(db, event("en", "language"), "123")
        await receive_messages(db, event("support", "start"), "123")
        await receive_messages(db, event("2", "inquiry"), "123")
    async with db_session_factory() as db:

        async def fail_commit():
            raise RuntimeError("simulated database failure")

        with monkeypatch.context() as patch:
            patch.setattr(db, "commit", fail_commit)
            with pytest.raises(RuntimeError):
                await receive_messages(
                    db, event("Please check my delivery", "details"), "123"
                )
        await db.rollback()
    async with db_session_factory() as db:
        result = await db.execute(
            select(WhatsAppReply).where(WhatsAppReply.message_id == prefix + "details")
        )
        assert result.scalar_one_or_none() is None
        await receive_messages(db, event("Please check my delivery", "details"), "123")
        ticket = (
            await db.execute(
                select(SupportTicket).where(SupportTicket.contact_phone == "+" + phone)
            )
        ).scalar_one()
        assert ticket.ticket_id is not None


async def test_staff_chat_queue_idempotency_scope_and_receipts(
    db_session_factory, whatsapp_case, make_admin, api, monkeypatch
):
    from datetime import datetime, timedelta, timezone

    from app.config.settings import settings
    from app.services.whatsapp_menu import text_reply

    phone, _, event = whatsapp_case
    monkeypatch.setattr(settings, "whatsapp_enabled", True)
    monkeypatch.setattr(settings, "whatsapp_access_token", "test-only-token")
    staff = await make_admin(api=api)
    other_staff = await make_admin(api=api)
    branch_staff = await make_admin("branch_manager", api=api)
    async with db_session_factory() as db:
        await receive_messages(db, event("en", "language"), "123")
        await receive_messages(db, event("support", "start"), "123")
        await receive_messages(db, event("2", "inquiry"), "123")
        await receive_messages(
            db, event("Please help with my delivery", "details"), "123"
        )
        conversation = await db.get(WhatsAppConversation, phone)
        ticket_id = str(conversation.ticket_id)
    base = f"/api/v1/admin/support/tickets/{ticket_id}/whatsapp"
    assert (await api.get(base, headers=branch_staff.headers)).status_code == 403
    assert (await api.post(base + "/claim", headers=staff.headers)).status_code == 200
    assert (
        await api.post(base + "/claim", headers=other_staff.headers)
    ).status_code == 409
    payload = {
        "request_id": str(uuid.uuid4()),
        "body": "We are checking your delivery.",
    }

    async def send():
        return await api.post(base + "/messages", json=payload, headers=staff.headers)

    responses = await asyncio.gather(send(), send(), send())
    assert all(response.status_code == 200 for response in responses), [
        r.text for r in responses
    ]
    assert len({response.json()["id"] for response in responses}) == 1
    queued_id = uuid.UUID(responses[0].json()["id"])
    history = (await api.get(base, headers=staff.headers)).json()
    assert history["staff_active"]
    assert len([row for row in history["messages"] if row["is_staff"]]) == 1
    async with db_session_factory() as db:
        row = await db.get(WhatsAppReply, queued_id)
        assert row.payload == text_reply(payload["body"])
        assert row.status == "pending"
        receipt = event("", "unused")
        value = receipt["entry"][0]["changes"][0]["value"]
        value.pop("messages")
        value["statuses"] = [
            {
                "id": "wamid." + str(queued_id),
                "recipient_id": phone,
                "status": "read",
                "biz_opaque_callback_data": str(queued_id),
            }
        ]
        await receive_messages(db, receipt, "123")
        value["statuses"][0]["status"] = "sent"
        await receive_messages(db, receipt, "123")
        await db.refresh(row)
        assert row.status == "read"
        conversation = await db.get(WhatsAppConversation, phone)
        conversation.last_message_at = datetime.now(timezone.utc) - timedelta(hours=25)
        await db.commit()
    expired = await api.post(
        base + "/messages",
        json={**payload, "request_id": str(uuid.uuid4())},
        headers=staff.headers,
    )
    assert expired.status_code == 409
    # Retrying an accepted request after the window ends returns its original ID.
    assert (await send()).json()["id"] == str(queued_id)
    assert (
        await api.post(base + "/release", headers=other_staff.headers)
    ).status_code == 409
    assert (await api.post(base + "/release", headers=staff.headers)).status_code == 200


async def test_history_cursor_is_ticket_scoped(
    db_session_factory, whatsapp_case, make_admin, api
):
    phone, _, event = whatsapp_case
    staff = await make_admin(api=api)
    async with db_session_factory() as db:
        await receive_messages(db, event("en", "language"), "123")
        await receive_messages(db, event("support", "start"), "123")
        await receive_messages(db, event("2", "inquiry"), "123")
        await receive_messages(db, event("Please help with my order", "details"), "123")
        for number in range(4):
            await receive_messages(
                db, event(f"Follow-up {number}", f"follow{number}"), "123"
            )
        conversation = await db.get(WhatsAppConversation, phone)
        ticket_id = str(conversation.ticket_id)
        menu_row = (
            (
                await db.execute(
                    select(WhatsAppReply).where(
                        WhatsAppReply.phone == phone, WhatsAppReply.ticket_id.is_(None)
                    )
                )
            )
            .scalars()
            .first()
        )
        other_cursor = str(menu_row.reply_id)
    base = f"/api/v1/admin/support/tickets/{ticket_id}/whatsapp"
    page = (await api.get(base, params={"limit": 2}, headers=staff.headers)).json()
    next_page = (
        await api.get(
            base,
            params={"limit": 2, "before": page["next_before"]},
            headers=staff.headers,
        )
    ).json()
    assert len(page["messages"]) == len(next_page["messages"]) == 2
    assert not (
        {row["id"] for row in page["messages"]}
        & {row["id"] for row in next_page["messages"]}
    )
    assert (
        await api.get(base, params={"before": other_cursor}, headers=staff.headers)
    ).status_code == 400


async def test_verified_account_order_and_ticket_ownership(db, make_customer):
    from decimal import Decimal

    from app.models.order import Order
    from app.services.whatsapp_support import owned_order, owned_ticket
    customer = await make_customer()
    other = await make_customer()
    number = "FC-WA-" + uuid.uuid4().hex[:12].upper()
    order = Order(user_id=customer.user_id, order_number=number,
        subtotal=Decimal("100"), total_amount=Decimal("100"),
        status="confirmed", payment_status="pending")
    ticket = SupportTicket(ticket_number="TKT-20261111-" + uuid.uuid4().hex[:5].upper(),
        user_id=customer.user_id, contact_phone=customer.phone, category="general",
        message="Synthetic WhatsApp ownership test", status="pending")
    db.add_all([order, ticket])
    await db.commit()
    assert (await owned_order(db, customer.phone, number)).order_id == order.order_id
    assert await owned_order(db, other.phone, number) is None
    assert (await owned_ticket(db, customer.phone, ticket.ticket_number)).ticket_id == ticket.ticket_id
    assert await owned_ticket(db, other.phone, ticket.ticket_number) is None


async def test_new_and_saved_language_preferences(db_session_factory, whatsapp_case):
    phone, _, event = whatsapp_case
    async with db_session_factory() as db:
        await receive_messages(db, event("hello", "hello"), "123")
        conversation = await db.get(WhatsAppConversation, phone)
        assert not conversation.language_selected
        await receive_messages(db, event("lang_ta", "tamil"), "123")
    async with db_session_factory() as db:
        conversation = await db.get(WhatsAppConversation, phone)
        assert conversation.language == "ta" and conversation.language_selected
        await receive_messages(db, event("orders", "orders"), "123")
        await receive_messages(db, event("language", "change"), "123")
        await receive_messages(db, event("lang_si", "sinhala"), "123")
    async with db_session_factory() as db:
        conversation = await db.get(WhatsAppConversation, phone)
        assert conversation.language == "si"
