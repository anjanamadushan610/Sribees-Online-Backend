"""
Push notifications end to end: a device registers its token, the token
rotates, moves between accounts and is removed; a campaign reaches the right
customers in their language, and a token FCM calls dead stops being used.

FCM itself is the only thing replaced: send_to_tokens records what would have
gone out and answers the way FCM does, with one token reported invalid.
"""
from __future__ import annotations

import asyncio
import uuid

from sqlalchemy import select

URL = "/api/v1/notifications"


def _token() -> str:
    return f"zz-fcm-{uuid.uuid4().hex}"


async def _register(api, customer, token, device_id=None, **extra):
    body = {"token": token, "platform": "android", **({"device_id": device_id} if device_id else {}), **extra}
    r = await api.post(f"{URL}/push/token", json=body, headers=customer.headers)
    assert r.status_code == 200, r.text
    return r.json()["data"]


async def _rows(db, user_id):
    from app.models.notification import PushToken

    db.expire_all()
    return (await db.execute(select(PushToken).where(PushToken.user_id == user_id))).scalars().all()


async def test_a_device_token_rotates_in_place(api, db, world, make_customer):
    c = await make_customer()
    device = f"zz-device-{uuid.uuid4().hex[:8]}"
    first = await _register(api, c, _token(), device, language="si")
    assert first["language"] == "si" and first["is_active"] is True
    rotated = _token()
    second = await _register(api, c, rotated, device)
    assert second["token_id"] == first["token_id"], "the same device keeps one row"
    rows = await _rows(db, c.user_id)
    assert [r.token for r in rows] == [rotated]

    # The app's language follows the customer onto their in-app rows.
    from app.models.user import User

    db.expire_all()
    assert (await db.get(User, c.user_id)).preferred_language == "si"


async def test_a_token_already_held_elsewhere_moves(api, db, world, make_customer):
    a, b = await make_customer(), await make_customer()
    shared = _token()
    await _register(api, a, shared)  # no device id: a bare token row
    # The phone changes hands: the same token now registers for b.
    moved = await _register(api, b, shared, "zz-new-owner")
    assert moved["device_id"] == "zz-new-owner"
    assert await _rows(db, a.user_id) == []
    assert [r.token for r in await _rows(db, b.user_id)] == [shared]

    # b's device already has a row and rotates onto a token a's old row holds:
    # the stale holder is dropped rather than tripping the unique constraint.
    stale = _token()
    await _register(api, a, stale, "zz-a-device")
    await _register(api, b, stale, "zz-new-owner")
    assert await _rows(db, a.user_id) == []


async def test_the_language_header_is_the_fallback(api, world, make_customer):
    c = await make_customer()
    r = await api.post(f"{URL}/push/token", json={"token": _token()},
                       headers={**c.headers, "Accept-Language": "ta"})
    assert r.status_code == 200 and r.json()["data"]["language"] == "ta"


async def test_the_legacy_token_routes(api, world, make_customer):
    c = await make_customer()
    token = _token()
    r = await api.post(f"{URL}/push-token", json={"token": token, "device_type": "ios", "device_name": "ZZ"},
                       headers=c.headers)
    assert r.status_code == 200, r.text
    r = await api.post(f"{URL}/push-token", json={"token": token, "device_type": "ios"}, headers=c.headers)
    assert r.status_code == 200, "registering the same token again refreshes it"
    r = await api.delete(f"{URL}/push-token/{token}", headers=c.headers)
    assert r.status_code == 200, r.text
    r = await api.delete(f"{URL}/push-token/{_token()}", headers=c.headers)
    assert r.status_code == 404, "only a token this customer registered can be removed"


async def test_a_campaign_reaches_its_audience(api, db, world, make_admin, make_customer, make_product,
                                               place_order, monkeypatch):
    from app.models.notification import Notification, NotificationCampaign, PushToken
    from app.services import campaign_service
    from app.services.fcm_service import FCMService

    sent: list[dict] = []
    dead = _token()

    async def fake_send(cls, tokens, title, body, data=None, image_url=None):
        sent.append({"tokens": list(tokens), "title": title, "body": body, "data": data, "image": image_url})
        bad = [t for t in tokens if t == dead]
        return {"success_count": len(tokens) - len(bad), "failure_count": len(bad), "invalid_tokens": bad}

    monkeypatch.setattr(FCMService, "send_to_tokens", classmethod(fake_send))

    customer = await make_customer()
    await place_order(customer, await make_product())  # ordered here: in the branch's audience
    live = _token()
    await _register(api, customer, live, "zz-live", language="si")
    await _register(api, customer, dead, "zz-dead", language="en")
    outsider = await make_customer()  # never ordered from this branch
    await _register(api, outsider, _token(), "zz-outsider")

    boss = await make_admin("super_admin", api=api)
    body = {
        "category": "offers",
        "title": {"en": "ZZ Rice week", "si": "ZZ සහල් සතිය"},
        "message": {"en": "Ten percent off rice", "si": "සහල් 10% අඩුවෙන්"},
        "image_url": "https://cdn.example.test/rice.jpg",
        "link_type": "offers",
        "branch_id": str(world.branch_id),
    }
    r = await api.post("/api/v1/admin/notification-campaigns", json=body, headers=boss.headers)
    assert r.status_code == 201, r.text
    cid = uuid.UUID(r.json()["data"]["campaign_id"])

    # "Send now" hands off to the worker; run it here too and wait for it.
    await campaign_service.run_due()
    for _ in range(50):
        db.expire_all()
        campaign = await db.get(NotificationCampaign, cid)
        if campaign.status == "sent":
            break
        await asyncio.sleep(0.1)
    assert campaign.status == "sent", campaign.status
    assert campaign.recipients == 1 and campaign.devices == 2
    assert campaign.delivered == 1 and campaign.failed == 1

    tokens_sent = {t for s in sent for t in s["tokens"]}
    assert live in tokens_sent and dead in tokens_sent
    assert all(s["image"] == "https://cdn.example.test/rice.jpg" for s in sent)
    assert any("සහල්" in s["body"] for s in sent), "each device hears it in its own language"

    inbox = (await db.execute(select(Notification).where(Notification.reference_id == cid))).scalars().all()
    assert [n.user_id for n in inbox] == [customer.user_id]
    row = (await db.execute(select(PushToken).where(PushToken.token == dead))).scalar_one()
    assert row.is_active is False, "a token FCM calls invalid is not used again"

    # Opening it counts once per customer; a finished campaign cannot be cancelled.
    r = await api.post(f"{URL}/campaigns/{cid}/opened", headers=customer.headers)
    assert r.status_code == 200, r.text
    r = await api.post(f"/api/v1/admin/notification-campaigns/{cid}/cancel", headers=boss.headers)
    assert r.status_code == 409


async def test_a_scheduled_campaign_can_be_cancelled(api, world, make_admin):
    from datetime import datetime, timedelta, timezone

    boss = await make_admin("super_admin", api=api)
    base = {"category": "general", "title": {"en": "ZZ Later"}, "message": {"en": "Soon"}}
    later = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()
    r = await api.post("/api/v1/admin/notification-campaigns", json={**base, "scheduled_at": later}, headers=boss.headers)
    assert r.status_code == 201 and r.json()["message"] == "Scheduled", r.text
    cid = r.json()["data"]["campaign_id"]
    r = await api.post(f"/api/v1/admin/notification-campaigns/{cid}/cancel", headers=boss.headers)
    assert r.status_code == 200 and r.json()["data"]["status"] == "cancelled", r.text

    too_far = (datetime.now(timezone.utc) + timedelta(days=45)).isoformat()
    for extra, why in [
        ({"scheduled_at": too_far}, "more than 30 days ahead"),
        ({"scheduled_at": "2030-01-01T10:00:00"}, "no timezone"),
        ({"link_type": "product", "link_value": str(uuid.uuid4())}, "a product that does not exist"),
        ({"branch_id": str(uuid.uuid4())}, "a branch that does not exist"),
    ]:
        r = await api.post("/api/v1/admin/notification-campaigns", json={**base, **extra}, headers=boss.headers)
        assert r.status_code == 422, (why, r.text)
    r = await api.post(f"/api/v1/admin/notification-campaigns/{uuid.uuid4()}/cancel", headers=boss.headers)
    assert r.status_code == 404
