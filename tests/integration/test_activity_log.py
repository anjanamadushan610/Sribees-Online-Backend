"""
Customer activity log (docs/plans/customer-activity-log.md): the app and web
send batches, guests too; Super Admin reads one timeline per customer that
includes what that install did before signing in.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

BATCH = "/api/v1/app/activity/batch"
TIMELINE = "/api/v1/admin/activity"


def _device() -> str:
    return f"guest_{uuid.uuid4().hex[:20]}"


def _batch(device, events, platform="android", **over) -> dict:
    return {"device_id": device, "platform": platform, "app_version": "1.2.10", "session_id": "s1",
            "events": events, **over}


def _at(minutes_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat()


async def _timeline(api, boss, **params) -> dict:
    r = await api.get(TIMELINE, params=params, headers=boss.headers)
    assert r.status_code == 200, r.text
    return r.json()["data"]


async def test_guest_browsing_shows_on_the_customer_after_sign_in(api, make_admin, make_customer, make_product):
    boss = await make_admin("super_admin", api=api)
    customer = await make_customer(with_address=False)
    product_id = await make_product()
    device = _device()

    # As a guest: open, look at a product, swipe its photos, scroll a screen.
    r = await api.post(BATCH, json=_batch(device, [
        {"event": "app_open", "occurred_at": _at(30)},
        {"event": "product_view", "product_id": str(product_id), "props": {"source": "home"}, "occurred_at": _at(29)},
        {"event": "image_swipe", "product_id": str(product_id), "props": {"index": 2, "total": 4}, "occurred_at": _at(28)},
        {"event": "screen", "screen": "home", "props": {"seconds": 95, "scroll_pct": 80, "items_seen": 24},
         "occurred_at": _at(27)},
    ]))
    assert r.status_code == 202, r.text
    assert r.json()["data"]["accepted"] == 4

    # Signs in on the same phone and searches.
    r = await api.post(BATCH, json=_batch(device, [
        {"event": "sign_up", "props": {"method": "google"}, "occurred_at": _at(5)},
        {"event": "search", "props": {"query": "rice", "results": 12}, "occurred_at": _at(4)},
    ]), headers=customer.headers)
    assert r.status_code == 202, r.text

    data = await _timeline(api, boss, user_id=str(customer.user_id))
    names = [e["event"] for e in data["events"]]
    assert names == ["search", "sign_up", "screen", "image_swipe", "product_view", "app_open"], names
    guest_view = next(e for e in data["events"] if e["event"] == "product_view")
    assert guest_view["is_guest"] is True and guest_view["product_name"]
    assert data["events"][0]["is_guest"] is False
    assert data["events"][0]["occurred_at_lk"], "Colombo time for the page"


async def test_another_customers_events_on_a_shared_phone_stay_theirs(api, make_admin, make_customer):
    boss = await make_admin("super_admin", api=api)
    first, second = await make_customer(with_address=False), await make_customer(with_address=False)
    device = _device()
    await api.post(BATCH, json=_batch(device, [{"event": "app_open"}]), headers=first.headers)
    await api.post(BATCH, json=_batch(device, [{"event": "search", "props": {"query": "milk"}}]),
                   headers=second.headers)

    mine = await _timeline(api, boss, user_id=str(first.user_id))
    assert [e["event"] for e in mine["events"]] == ["app_open"]
    whole_phone = await _timeline(api, boss, device_id=device)
    assert len(whole_phone["events"]) == 2


async def test_the_body_cannot_name_a_customer_or_send_unknown_things(api):
    device = _device()
    r = await api.post(BATCH, json=_batch(device, [{"event": "app_open"}], user_id=str(uuid.uuid4())))
    assert r.status_code == 422, "user_id comes from the token only"
    r = await api.post(BATCH, json=_batch(device, [{"event": "made_up_event"}]))
    assert r.status_code == 422 and "unknown event" in r.text
    r = await api.post(BATCH, json=_batch(device, [{"event": "app_open"}] * 101))
    assert r.status_code == 422
    r = await api.post(BATCH, json=_batch(device, [{"event": "tap", "props": {"nested": {"a": 1}}}]))
    assert r.status_code == 422
    r = await api.post(BATCH, json=_batch(device, [{"event": "app_open"}], platform="windows"))
    assert r.status_code == 422


async def test_secrets_are_dropped_and_wild_clocks_are_clamped(api, make_admin):
    boss = await make_admin("super_admin", api=api)
    device = _device()
    future = (datetime.now(timezone.utc) + timedelta(days=3)).isoformat()
    ancient = (datetime.now(timezone.utc) - timedelta(days=400)).isoformat()
    r = await api.post(BATCH, json=_batch(device, [
        {"event": "tap", "props": {"target": "login", "password": "hunter2", "otp": "1234"}, "occurred_at": future},
        {"event": "app_open", "occurred_at": ancient},
    ]))
    assert r.status_code == 202, r.text

    events = (await _timeline(api, boss, device_id=device))["events"]
    tap = next(e for e in events if e["event"] == "tap")
    assert tap["props"] == {"target": "login"}
    now = datetime.now(timezone.utc)
    for e in events:
        at = datetime.fromisoformat(e["occurred_at"])
        assert now - timedelta(days=15) < at <= now + timedelta(minutes=11)


async def test_only_super_admin_reads_it_and_it_pages(api, make_admin, make_customer):
    boss = await make_admin("super_admin", api=api)
    customer = await make_customer(with_address=False)
    device = _device()

    r = await api.get(TIMELINE, params={"device_id": device}, headers=customer.headers)
    assert r.status_code in (401, 403)
    r = await api.get(TIMELINE, headers=boss.headers)
    assert r.status_code == 400

    await api.post(BATCH, json=_batch(device, [{"event": "tap", "props": {"target": f"b{i}"},
                                                 "occurred_at": _at(10 - i)} for i in range(5)]))
    first = await _timeline(api, boss, device_id=device, limit=3)
    assert len(first["events"]) == 3 and first["has_more"] is True
    rest = await _timeline(api, boss, device_id=device, limit=3, before_id=first["next_before_id"])
    assert len(rest["events"]) == 2 and rest["has_more"] is False
    seen = [e["props"]["target"] for e in first["events"] + rest["events"]]
    assert seen == ["b4", "b3", "b2", "b1", "b0"]


async def test_closing_the_account_deletes_its_activity(api, db, make_customer):
    from sqlalchemy import func, select

    from app.models.activity import ActivityEvent
    from app.services.account_deletion_service import close_account

    customer = await make_customer(with_address=False)
    await api.post(BATCH, json=_batch(_device(), [{"event": "app_open"}]), headers=customer.headers)
    count = select(func.count()).select_from(ActivityEvent).where(ActivityEvent.user_id == customer.user_id)
    assert (await db.execute(count)).scalar() == 1

    await close_account(db, customer.user_id)
    assert (await db.execute(count)).scalar() == 0
