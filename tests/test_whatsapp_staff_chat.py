"""Staff authorization, reply-window and receipt regression checks."""

import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from app.api.v1 import admin_whatsapp
from app.config.settings import settings
from app.models.whatsapp import WhatsAppReply
from app.services.whatsapp_menu import text_reply
from app.services.whatsapp_receipts import advances_status, receive_statuses
from tests.test_whatsapp_support import MemoryDB, envelope


@pytest.mark.parametrize(
    "current,incoming,expected",
    [
        ("pending", "sent", True),
        ("uncertain", "delivered", True),
        ("read", "sent", False),
        ("delivered", "failed", False),
        ("accepted", "failed", True),
        ("failed", "read", True),
        ("cancelled", "sent", False),
        ("read", "read", False),
        ("accepted", "unknown", False),
    ],
)
def test_receipts_never_regress(current, incoming, expected):
    assert advances_status(current, incoming) is expected


async def test_staff_handoff_preserves_all_messages_until_staff_releases():
    from app.services.whatsapp_service import receive_messages

    db = MemoryDB()
    await receive_messages(db, envelope("en", message_id="language"), "123")
    await receive_messages(db, envelope("support", message_id="start"), "123")
    await receive_messages(db, envelope("2", message_id="new"), "123")
    await receive_messages(
        db, envelope("Please help with my order", message_id="issue"), "123"
    )
    conversation = db.conversations["94760000000"]
    ticket_id = conversation.ticket_id
    conversation.staff_active = True
    await receive_messages(db, envelope("hi", message_id="followup"), "123")
    assert conversation.ticket_id == ticket_id
    assert db.messages["followup"].ticket_id == ticket_id
    assert db.messages["followup"].payload is None
    await receive_messages(db, envelope("MENU", message_id="exit"), "123")
    assert conversation.ticket_id == ticket_id
    assert conversation.staff_active
    assert db.messages["exit"].payload is None


@pytest.fixture
def context(monkeypatch):
    admin = SimpleNamespace(admin_id=uuid.uuid4(), role="super_admin", branch_id=None)
    ticket = SimpleNamespace(
        ticket_id=uuid.uuid4(), status="in_progress", assigned_admin_id=admin.admin_id
    )
    conversation = SimpleNamespace(
        phone="94760000000",
        staff_active=True,
        last_message_at=datetime.now(timezone.utc),
        ticket_id=ticket.ticket_id,
    )
    monkeypatch.setattr(
        admin_whatsapp, "ticket_context", AsyncMock(return_value=(ticket, conversation))
    )
    monkeypatch.setattr(settings, "whatsapp_enabled", True)
    monkeypatch.setattr(settings, "whatsapp_access_token", "test-token")
    db = SimpleNamespace(
        execute=AsyncMock(
            return_value=SimpleNamespace(scalar_one_or_none=lambda: None)
        ),
        add=lambda row: None,
        commit=AsyncMock(),
        refresh=AsyncMock(),
    )
    return admin, ticket, conversation, db


@pytest.mark.parametrize(
    "scenario,expected",
    [
        ("expired", 409),
        ("unclaimed", 409),
        ("other_staff", 409),
        ("closed", 409),
        ("disabled", 503),
        ("blank", 422),
    ],
)
async def test_unsafe_staff_sends_rejected(context, scenario, expected):
    admin, ticket, conversation, db = context
    body = "Test reply"
    if scenario == "expired":
        conversation.last_message_at -= timedelta(hours=24)
    elif scenario == "unclaimed":
        conversation.staff_active = False
    elif scenario == "other_staff":
        ticket.assigned_admin_id = uuid.uuid4()
    elif scenario == "closed":
        ticket.status = "closed"
    elif scenario == "disabled":
        settings.whatsapp_enabled = False
    else:
        body = "   "
    with pytest.raises(HTTPException) as error:
        await admin_whatsapp.send_reply(
            ticket.ticket_id,
            admin_whatsapp.StaffReplyRequest(request_id=uuid.uuid4(), body=body),
            db,
            admin,
        )
    assert error.value.status_code == expected
    db.commit.assert_not_called()


async def test_retry_returns_existing_without_resending(context):
    admin, ticket, conversation, db = context
    conversation.last_message_at -= timedelta(days=2)
    row = WhatsAppReply(
        reply_id=uuid.uuid4(),
        admin_id=admin.admin_id,
        payload=text_reply("Hello"),
        created_at=datetime.now(timezone.utc),
        is_staff=True,
        status="accepted",
    )
    db.execute.return_value = SimpleNamespace(scalar_one_or_none=lambda: row)
    result = await admin_whatsapp.send_reply(
        ticket.ticket_id,
        admin_whatsapp.StaffReplyRequest(request_id=uuid.uuid4(), body="Hello"),
        db,
        admin,
    )
    assert result["id"] == str(row.reply_id)
    db.commit.assert_not_called()


async def test_ticket_scope_blocks_branch_admin():
    ticket = SimpleNamespace(branch_id=uuid.uuid4())
    admin = SimpleNamespace(role="branch_manager", branch_id=uuid.uuid4())
    db = SimpleNamespace(get=AsyncMock(return_value=ticket))
    with pytest.raises(HTTPException) as error:
        await admin_whatsapp.ticket_context(db, admin, uuid.uuid4())
    assert error.value.status_code == 403


async def test_receipt_requires_matching_recipient():
    row = WhatsAppReply(phone="94760000000", status="accepted")
    db = SimpleNamespace(
        execute=AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda: row)),
        flush=AsyncMock(),
    )
    payload = envelope()
    value = payload["entry"][0]["changes"][0]["value"]
    value["statuses"] = [
        {"id": "wamid.outgoing", "recipient_id": "94761111111", "status": "read"}
    ]
    await receive_statuses(db, payload, "123")
    assert row.status == "accepted"
    value["statuses"][0]["recipient_id"] = row.phone
    await receive_statuses(db, payload, "123")
    assert row.status == "read"
