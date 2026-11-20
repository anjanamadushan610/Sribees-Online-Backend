"""SRIBEES Partner app pushes: templates, routing, device ownership, never-raise."""
import re
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from app.services import partner_push
from tests.conftest import requires_db


def test_every_template_has_three_languages_with_same_placeholders():
    for kind, langs in partner_push.TEMPLATES.items():
        assert set(langs) == {"en", "si", "ta"}, kind
        names = {lang: set(re.findall(r"{(\w+)}", text)) for lang, text in langs.items()}
        assert names["en"] == names["si"] == names["ta"], kind
        assert kind in partner_push.SCREENS


def test_render_falls_back_to_english_and_fills_values():
    text = partner_push.render("withdrawal_paid", "fr", amount="Rs. 1,000.00")
    assert text.startswith("Your withdrawal of Rs. 1,000.00")
    assert "Rs. 1,000.00" in partner_push.render("withdrawal_paid", "si-LK", amount="Rs. 1,000.00")


@pytest.mark.asyncio
async def test_notify_partner_never_raises_and_uses_its_own_session(monkeypatch):
    import app.config.database as database

    def boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(database, "async_session_maker", boom)
    assert await partner_push.notify_partner(uuid4(), "customer_joined") == 0
    assert await partner_push.notify_partner(None, "customer_joined") == 0


@pytest.mark.asyncio
async def test_earnings_ready_is_one_push_per_partner_with_total(monkeypatch):
    calls = []

    async def fake_notify(partner_id, kind, **values):
        calls.append((partner_id, kind, values))
        return 1

    monkeypatch.setattr(partner_push, "notify_partner", fake_notify)
    p1, p2 = uuid4(), uuid4()
    o1, o2 = uuid4(), uuid4()
    rows = [
        SimpleNamespace(beneficiary_partner_id=p1, amount=Decimal("10.50"), order_id=o1),
        SimpleNamespace(beneficiary_partner_id=p1, amount=Decimal("4.50"), order_id=o2),
        SimpleNamespace(beneficiary_partner_id=p2, amount=Decimal("0"), order_id=o1),
        SimpleNamespace(beneficiary_partner_id=None, amount=Decimal("9"), order_id=o1),
    ]
    assert await partner_push.notify_earnings_ready(rows) == 1
    assert calls == [(p1, "earnings_ready", {"amount": "Rs. 15.00", "orders": 2})]


@pytest.mark.asyncio
async def test_withdrawal_decision_picks_the_right_message(monkeypatch):
    seen = []

    async def fake_notify(partner_id, kind, **values):
        seen.append((kind, values["amount"]))
        return 1

    monkeypatch.setattr(partner_push, "notify_partner", fake_notify)
    await partner_push.notify_withdrawal_decided(uuid4(), "paid", Decimal("500"))
    await partner_push.notify_withdrawal_decided(uuid4(), "rejected", Decimal("500"))
    assert seen == [("withdrawal_paid", "Rs. 500.00"), ("withdrawal_rejected", "Rs. 500.00")]


@pytest.mark.asyncio
async def test_device_routes_require_a_partner_token():
    import httpx
    from fastapi import FastAPI

    from app.api.v1.partner import router

    app = FastAPI()
    app.include_router(router, prefix="/partner")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/partner/devices", json={"token": "x" * 40})
    assert response.status_code in (401, 403)


@pytest.fixture
async def partner_db(db_schema):
    from app.config.settings import settings
    from app.models.partner import Partner

    engine = create_async_engine(settings.async_database_url, poolclass=NullPool)
    async with engine.connect() as connection:
        outer = await connection.begin()
        async with AsyncSession(bind=connection, expire_on_commit=False, join_transaction_mode="create_savepoint") as db:
            partners = []
            for _ in range(2):
                suffix = uuid4().hex[:8]
                partner = Partner(role="partner", email=f"{suffix}@example.test", full_name="Push Test",
                                  password_hash="unused", referral_code=f"P{suffix}".upper(), is_active=True)
                db.add(partner)
                partners.append(partner)
            await db.flush()
            yield db, partners
        await outer.rollback()
    await engine.dispose()


@requires_db
@pytest.mark.asyncio
async def test_register_moves_token_and_unregister_is_owner_only(partner_db, monkeypatch):
    from app.models.partner_device import PartnerDevice
    from app.services.fcm_service import FCMService

    db, (a, b) = partner_db
    token = "tok-" + uuid4().hex * 2
    await partner_push.register_device(db, a, token, "android", "si", "1.0.0+1")
    await partner_push.register_device(db, b, token, "android", "ta", "1.0.0+1")
    row = (await db.execute(select(PartnerDevice).where(PartnerDevice.token == token))).scalar_one()
    assert row.partner_id == b.partner_id and row.locale == "ta" and row.is_active

    # A cannot switch off B's phone by knowing the token.
    await partner_push.unregister_device(db, a.partner_id, token)
    await db.refresh(row)
    assert row.is_active

    sent = []

    async def fake_send(tokens, title, body, data=None, **kwargs):
        sent.append((tokens, body, data, kwargs.get("channel_id")))
        return {"success_count": len(tokens), "invalid_tokens": list(tokens)}

    monkeypatch.setattr(FCMService, "send_to_tokens", fake_send)
    # A password change silences phones registered under the old password.
    from datetime import datetime, timezone
    b.password_changed_at = datetime.now(timezone.utc)
    await db.flush()
    assert await partner_push._notify(db, b.partner_id, "customer_joined") == 0
    await partner_push.register_device(db, b, token, "android", "ta", "1.0.0+1")
    # A deactivated partner gets nothing.
    b.is_active = False
    await db.flush()
    assert await partner_push._notify(db, b.partner_id, "customer_joined") == 0
    b.is_active = True
    await db.flush()
    assert sent == []

    assert await partner_push._notify(db, b.partner_id, "customer_joined") == 1
    assert sent[0][0] == [token]
    assert sent[0][1] == partner_push.TEMPLATES["customer_joined"]["ta"]
    assert sent[0][2] == {"type": "customer_joined", "screen": "customers"}
    assert sent[0][3] == partner_push.PARTNER_CHANNEL_ID
    # FCM said the token is dead: it is switched off.
    await db.refresh(row)
    assert not row.is_active
    assert await partner_push._notify(db, a.partner_id, "customer_joined") == 0

    await partner_push.register_device(db, b, token, "android", "en", None)
    await partner_push.unregister_device(db, b.partner_id, token)
    await db.refresh(row)
    assert not row.is_active
