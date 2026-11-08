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
        await receive_messages(db, event("support", "start"), "123")

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
    payload = event("support", "start")
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
        await receive_messages(db, event("support", "start"), "123")
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
