"""Webhook security, language, deduplication, handoff and safe send failures."""

import hashlib
import hmac
import json
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.dialects import postgresql

from app.api import whatsapp
from app.config.settings import settings
from app.models.support_ticket import SupportTicket
from app.models.whatsapp import WhatsAppConversation, WhatsAppReply
from app.services import whatsapp_service, whatsapp_worker
from app.services.whatsapp_menu import incoming_messages, menu, valid_signature

NOW = datetime.now(timezone.utc)


def envelope(body="hello", kind="text", message_id="wamid.test", age=0, phone_id="123"):
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "metadata": {"phone_number_id": phone_id},
                            "messages": [
                                {
                                    "id": message_id,
                                    "from": "94760000000",
                                    "type": kind,
                                    "timestamp": str(
                                        int((NOW - timedelta(seconds=age)).timestamp())
                                    ),
                                    "text": {"body": body},
                                }
                            ],
                        },
                    }
                ]
            }
        ],
    }


def test_signature_checks_exact_body():
    body = b'{"test":1}'
    signature = "sha256=" + hmac.new(b"test-secret", body, hashlib.sha256).hexdigest()
    assert valid_signature(body, signature, "test-secret")
    assert not valid_signature(body + b" ", signature, "test-secret")
    assert not valid_signature(body, signature, "")
    assert not valid_signature(body, "", "test-secret")


@pytest.mark.parametrize("age", [86101, -301])
def test_old_or_future_events_ignored(age):
    assert not list(incoming_messages(envelope(age=age), "123", NOW))


def test_other_phone_and_delivery_status_ignored():
    assert not list(incoming_messages(envelope(phone_id="999"), "123", NOW))
    assert not list(
        incoming_messages(
            {"object": "whatsapp_business_account", "entry": []}, "123", NOW
        )
    )


def test_interactive_buttons_and_length_limits():
    payload = envelope(kind="interactive")
    message = payload["entry"][0]["changes"][0]["value"]["messages"][0]
    message["interactive"] = {"button_reply": {"id": "support"}}
    assert list(incoming_messages(payload, "123", NOW))[0][3] == "support"
    assert len(list(incoming_messages(envelope("x" * 5000), "123", NOW))[0][3]) == 4096


@pytest.mark.parametrize("language", ["si", "en"])
def test_menu_fits_whatsapp_button_limits(language):
    result = menu(language)["interactive"]
    assert len(result["body"]["text"]) <= 1024
    assert len(result["action"]["buttons"]) == 3
    assert all(
        len(button["reply"]["title"]) <= 20 for button in result["action"]["buttons"]
    )


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setattr(settings, "whatsapp_enabled", True)
    monkeypatch.setattr(settings, "whatsapp_verify_token", "test-verify")
    monkeypatch.setattr(settings, "whatsapp_app_secret", "test-secret")
    monkeypatch.setattr(settings, "whatsapp_phone_number_id", "123")
    app = FastAPI()
    app.include_router(whatsapp.router)

    async def fake_db():
        yield object()

    app.dependency_overrides[whatsapp.get_db] = fake_db
    return app


async def test_verification_challenge_and_wrong_token(configured):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=configured), base_url="https://test"
    ) as client:
        response = await client.get(
            "/api/v1/whatsapp/webhook",
            params={
                "hub.mode": "subscribe",
                "hub.verify_token": "test-verify",
                "hub.challenge": "012345",
            },
        )
        assert response.status_code == 200 and response.text == "012345"
        assert (
            await client.get(
                "/api/v1/whatsapp/webhook?hub.mode=subscribe&hub.verify_token=wrong"
            )
        ).status_code == 403


async def test_post_rejects_unsigned_and_tampered_before_processing(
    configured, monkeypatch
):
    receive = AsyncMock()
    monkeypatch.setattr(whatsapp, "receive_messages", receive)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=configured), base_url="https://test"
    ) as client:
        response = await client.post("/api/v1/whatsapp/webhook", json=envelope())
        assert response.status_code == 403
        assert not receive.called


async def test_signed_event_waits_for_commit(configured, monkeypatch):
    receive = AsyncMock()
    monkeypatch.setattr(whatsapp, "receive_messages", receive)
    body = json.dumps(envelope()).encode()
    signature = "sha256=" + hmac.new(b"test-secret", body, hashlib.sha256).hexdigest()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=configured), base_url="https://test"
    ) as client:
        response = await client.post(
            "/api/v1/whatsapp/webhook",
            content=body,
            headers={"x-hub-signature-256": signature},
        )
        assert response.status_code == 200
        receive.assert_awaited_once()


async def test_disabled_and_oversized(configured, monkeypatch):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=configured), base_url="https://test"
    ) as client:
        assert (
            await client.post(
                "/api/v1/whatsapp/webhook", content=b"x" * (1024 * 1024 + 1)
            )
        ).status_code == 413
        monkeypatch.setattr(settings, "whatsapp_enabled", False)
        assert (await client.get("/api/v1/whatsapp/webhook")).status_code == 503


class MemoryDB:
    """Exercise state transitions without claiming to simulate database locks."""

    def __init__(self):
        self.messages = {}
        self.conversations = {}
        self.tickets = {}
        self.commits = 0

    async def execute(self, statement):
        if str(statement).startswith("SELECT pg_advisory"):
            return None
        values = statement.compile(dialect=postgresql.dialect()).params
        existing = values["message_id"] in self.messages
        identifier = uuid.uuid4()
        if not existing:
            self.messages[values["message_id"]] = WhatsAppReply(
                reply_id=identifier,
                **{key: value for key, value in values.items() if key != "reply_id"},
            )
        return SimpleNamespace(
            scalar_one_or_none=lambda: None if existing else identifier
        )

    async def get(self, model, identifier):
        if model is WhatsAppReply:
            return next(
                row for row in self.messages.values() if row.reply_id == identifier
            )
        if model is WhatsAppConversation:
            return self.conversations.get(identifier)
        return self.tickets.get(identifier)

    def add(self, row):
        if isinstance(row, WhatsAppConversation):
            self.conversations[row.phone] = row
        elif isinstance(row, SupportTicket):
            row.ticket_id = uuid.uuid4()
            self.tickets[row.ticket_id] = row

    async def flush(self):
        pass

    async def commit(self):
        self.commits += 1


async def test_duplicate_event_creates_one_reply():
    db = MemoryDB()
    await whatsapp_service.receive_messages(db, envelope(), "123")
    await whatsapp_service.receive_messages(db, envelope(), "123")
    assert len(db.messages) == 1
    assert next(iter(db.messages.values())).status == "pending"


async def test_support_ticket_and_followup_without_duplicate_bot_reply():
    db = MemoryDB()
    for number, body in enumerate(
        ["SI", "support", "My delivery has not arrived", "Please call me"]
    ):
        await whatsapp_service.receive_messages(
            db, envelope(body, message_id=f"wamid.{number}"), "123"
        )
    assert len(db.tickets) == 1
    ticket = next(iter(db.tickets.values()))
    assert (
        "My delivery has not arrived" in ticket.message
        and "Please call me" in ticket.message
    )
    assert ticket.user_id is None
    assert db.messages["wamid.3"].status == "ignored"
    assert db.conversations["94760000000"].language == "si"
    await whatsapp_service.receive_messages(
        db, envelope("My delivery has not arrived", message_id="wamid.2"), "123"
    )
    assert len(db.tickets) == 1


async def test_menu_resets_handoff_and_media_does_not_create_empty_ticket():
    db = MemoryDB()
    await whatsapp_service.receive_messages(db, envelope("support"), "123")
    await whatsapp_service.receive_messages(
        db, envelope(kind="image", message_id="image"), "123"
    )
    assert not db.tickets
    assert db.conversations["94760000000"].awaiting_details
    await whatsapp_service.receive_messages(
        db, envelope("MENU", message_id="menu"), "123"
    )
    assert not db.conversations["94760000000"].awaiting_details


class WorkerDB:
    def __init__(self, job):
        self.job = job
        self.updates = []
        self.commits = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def execute(self, statement):
        if statement.is_select:
            return SimpleNamespace(scalar_one_or_none=lambda: self.job)
        self.updates.append(statement.compile(dialect=postgresql.dialect()).params)

    async def commit(self):
        self.commits += 1


@pytest.mark.parametrize(
    "outcome,expected",
    [
        (200, "accepted"),
        (400, "failed"),
        (429, "pending"),
        (500, "uncertain"),
        ("timeout", "uncertain"),
    ],
)
async def test_send_outcomes_do_not_blindly_retry(monkeypatch, outcome, expected):
    job = SimpleNamespace(
        reply_id=uuid.uuid4(),
        phone="94760000000",
        payload=menu("en"),
        attempts=0,
        expires_at=NOW + timedelta(hours=1),
        status="pending",
    )
    db = WorkerDB(job)
    monkeypatch.setattr(whatsapp_worker, "async_session_maker", lambda: db)
    monkeypatch.setattr(settings, "whatsapp_graph_version", "v23.0")
    monkeypatch.setattr(settings, "whatsapp_phone_number_id", "123")
    monkeypatch.setattr(settings, "whatsapp_access_token", "test-token")

    def handler(request):
        assert db.commits >= 1 and job.status == "sending"
        if outcome == "timeout":
            raise httpx.ReadTimeout("test timeout", request=request)
        return httpx.Response(outcome, json={"messages": [{"id": "outgoing"}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert await whatsapp_worker.send_next(client)
    assert db.updates[-1]["status"] == expected


async def test_expired_reply_never_calls_meta(monkeypatch):
    job = SimpleNamespace(
        reply_id=uuid.uuid4(), expires_at=NOW - timedelta(seconds=1), status="pending"
    )
    db = WorkerDB(job)
    monkeypatch.setattr(whatsapp_worker, "async_session_maker", lambda: db)
    client = SimpleNamespace(post=AsyncMock())
    assert await whatsapp_worker.send_next(client)
    assert job.status == "expired"
    client.post.assert_not_called()
