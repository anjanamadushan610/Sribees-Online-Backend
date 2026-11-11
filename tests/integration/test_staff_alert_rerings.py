"""
Ring until accepted, on the real schema (owner, 2026-11-11).

Real checkout, real admins, real sweep; only FCM is replaced. Time is moved by
editing next_ring_at / window_started_at, the way the clock would.
"""
from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import delete, text, update

URL = "/api/v1/admin/push-tokens"


def _token() -> str:
    return f"zz-ring-fcm-{uuid.uuid4().hex}"


async def _settle() -> None:
    from app.services.notification_service import _background

    for _ in range(50):
        pending = [t for t in list(_background) if not t.done()]
        if not pending:
            return
        await asyncio.wait(pending, timeout=10)


@pytest.fixture
def fcm(monkeypatch):
    from app.services.fcm_service import FCMService

    class Fake:
        rings: list = []
        clears: list = []

    async def send_to_tokens(tokens, title, body, data=None, image_url=None, **kw):
        Fake.rings.append({"tokens": list(tokens), "data": data, **kw})
        return {"success_count": len(tokens), "failure_count": 0, "failed_tokens": [], "invalid_tokens": []}

    async def send_data_to_tokens(tokens, data):
        Fake.clears.append({"tokens": list(tokens), "data": data})
        return len(tokens)

    Fake.rings, Fake.clears = [], []
    monkeypatch.setattr(FCMService, "send_to_tokens", send_to_tokens)
    monkeypatch.setattr(FCMService, "send_data_to_tokens", send_data_to_tokens)
    return Fake


@pytest.fixture(autouse=True)
def _daytime(monkeypatch):
    """The cadence tests run in the day; the night rule has its own unit tests."""
    from app.services import staff_order_alert_service as svc

    monkeypatch.setattr(svc, "_quiet", lambda at: False)


@pytest.fixture(autouse=True)
async def _forget(db):
    yield
    from app.models.admin_push import AdminPushToken

    await db.rollback()
    await db.execute(delete(AdminPushToken).where(AdminPushToken.token.like("zz-ring-fcm-%")))
    await db.commit()


def _for(fcm, order_id):
    return [r for r in fcm.rings if r["data"] and r["data"].get("order_id") == str(order_id)]


async def _setup(api, make_admin, make_product, make_customer, place_order):
    manager = await make_admin("branch_manager", api=api)
    token = _token()
    r = await api.post(URL, json={"token": token, "device_id": f"zz-d-{uuid.uuid4().hex[:8]}"},
                       headers=manager.headers)
    assert r.status_code == 200, r.text
    pid, customer = await make_product(), await make_customer()
    placed = await place_order(customer, pid)
    await _settle()
    return token, uuid.UUID(str(placed["order_id"]))


async def _due(db, order_id, **values):
    from app.models.admin_push import AdminOrderAlert

    values.setdefault("next_ring_at", text("now() - interval '1 second'"))
    await db.execute(update(AdminOrderAlert).where(AdminOrderAlert.order_id == order_id).values(**values))
    await db.commit()


async def _alert(db, order_id):
    from app.models.admin_push import AdminOrderAlert

    db.expire_all()
    return await db.get(AdminOrderAlert, order_id)


async def test_first_ring_schedules_the_next_two_minutes_later(
    api, db, world, make_admin, make_product, make_customer, place_order, fcm
):
    token, order_id = await _setup(api, make_admin, make_product, make_customer, place_order)
    a = await _alert(db, order_id)
    assert a.alert_status == "pending" and a.rings == 1 and a.stopped_at is None
    gap = (a.next_ring_at - a.window_started_at).total_seconds()
    assert 115 <= gap <= 125
    first = _for(fcm, order_id)
    assert len(first) == 1 and first[0]["tag"] == f"new_order:{order_id}"


async def test_a_due_order_rings_again_and_is_rescheduled(
    api, db, world, make_admin, make_product, make_customer, place_order, fcm
):
    from app.services.staff_order_alert_service import StaffOrderAlertService

    token, order_id = await _setup(api, make_admin, make_product, make_customer, place_order)
    await _due(db, order_id)
    assert await StaffOrderAlertService.sweep(db) >= 1
    assert len(_for(fcm, order_id)) == 2
    a = await _alert(db, order_id)
    assert a.rings == 2 and a.next_ring_at is not None and a.stopped_at is None

    # Not due yet: nothing happens.
    await StaffOrderAlertService.sweep(db)
    assert len(_for(fcm, order_id)) == 2


async def test_confirming_stops_the_ringing_and_clears_the_phones(
    api, db, world, make_admin, make_product, make_customer, place_order, fcm
):
    from app.models.order import Order
    from app.services.staff_order_alert_service import StaffOrderAlertService

    token, order_id = await _setup(api, make_admin, make_product, make_customer, place_order)
    boss = await make_admin("super_admin", api=api)
    r = await api.patch(f"/api/v1/admin/orders/{order_id}/status", json={"status": "confirmed"},
                        headers=boss.headers)
    assert r.status_code == 200, r.text
    db.expire_all()
    assert (await db.get(Order, order_id)).status == "confirmed"

    await _due(db, order_id)
    await StaffOrderAlertService.sweep(db)
    assert len(_for(fcm, order_id)) == 1, "no ring after it was accepted"
    a = await _alert(db, order_id)
    assert a.stopped_at is not None and a.next_ring_at is None
    clear = [c for c in fcm.clears if c["data"]["order_id"] == str(order_id)]
    assert clear and token in clear[0]["tokens"]
    assert clear[0]["data"] == {"type": "order_accepted", "order_id": str(order_id),
                                "tag": f"new_order:{order_id}"}


async def test_a_card_order_that_arrived_confirmed_rings_until_packing(
    api, db, world, make_admin, make_product, make_customer, place_order, fcm
):
    from app.models.order import Order
    from app.services.staff_order_alert_service import StaffOrderAlertService

    token, order_id = await _setup(api, make_admin, make_product, make_customer, place_order)
    # As a paid card order is stored: it rang while 'confirmed'.
    await db.execute(update(Order).where(Order.order_id == order_id).values(status="confirmed"))
    await _due(db, order_id, alert_status="confirmed")
    await StaffOrderAlertService.sweep(db)
    assert len(_for(fcm, order_id)) == 2, "still confirmed = not yet accepted"

    await db.execute(update(Order).where(Order.order_id == order_id).values(status="processing"))
    await _due(db, order_id)
    await StaffOrderAlertService.sweep(db)
    assert len(_for(fcm, order_id)) == 2
    assert (await _alert(db, order_id)).stopped_at is not None


async def test_the_window_ends_after_thirty_minutes(
    api, db, world, make_admin, make_product, make_customer, place_order, fcm
):
    from app.services.staff_order_alert_service import StaffOrderAlertService

    token, order_id = await _setup(api, make_admin, make_product, make_customer, place_order)
    # Ring 6 at minute 29: only the 30-minute limit can stop it here.
    await _due(db, order_id, window_started_at=text("now() - interval '29 minutes'"), rings=5)
    await StaffOrderAlertService.sweep(db)
    assert len(_for(fcm, order_id)) == 2, "the last ring still goes out"
    a = await _alert(db, order_id)
    # Still not accepted: parked until the next 07:00 Colombo, a fresh window.
    assert a.stopped_at is None and a.rings == 0
    assert a.next_ring_at == a.window_started_at
    from zoneinfo import ZoneInfo

    local = a.next_ring_at.astimezone(ZoneInfo("Asia/Colombo"))
    assert (local.hour, local.minute) == (7, 0)


async def test_two_workers_ring_a_due_order_once(
    api, db, db_session_factory, world, make_admin, make_product, make_customer, place_order, fcm
):
    from app.services.staff_order_alert_service import StaffOrderAlertService

    token, order_id = await _setup(api, make_admin, make_product, make_customer, place_order)
    await _due(db, order_id)

    async def worker():
        async with db_session_factory() as s:
            return await StaffOrderAlertService.sweep(s)

    await asyncio.gather(worker(), worker(), worker())
    assert len(_for(fcm, order_id)) == 2


async def test_alerts_from_before_the_feature_never_ring(
    api, db, world, make_admin, make_product, make_customer, place_order, fcm
):
    from app.services.staff_order_alert_service import StaffOrderAlertService

    token, order_id = await _setup(api, make_admin, make_product, make_customer, place_order)
    # A row as migration 110 leaves an old alert: no schedule.
    await _due(db, order_id, next_ring_at=None, alert_status=None, rings=0)
    await StaffOrderAlertService.sweep(db)
    assert len(_for(fcm, order_id)) == 1


async def test_cancelling_stops_and_clears(
    api, db, world, make_admin, make_product, make_customer, place_order, fcm
):
    from app.models.order import Order
    from app.services.staff_order_alert_service import StaffOrderAlertService

    token, order_id = await _setup(api, make_admin, make_product, make_customer, place_order)
    await db.execute(update(Order).where(Order.order_id == order_id).values(status="cancelled"))
    await _due(db, order_id)
    await StaffOrderAlertService.sweep(db)
    assert len(_for(fcm, order_id)) == 1
    assert [c for c in fcm.clears if c["data"]["order_id"] == str(order_id)]
    assert (await _alert(db, order_id)).cleared_at is not None


async def test_accepted_after_the_window_ended_is_still_cleared(
    api, db, world, make_admin, make_product, make_customer, place_order, fcm
):
    """Ringing stopped at 30 minutes; someone confirms an hour later: the
    phones still drop the notification -- not left there for good."""
    from app.models.order import Order
    from app.services.staff_order_alert_service import StaffOrderAlertService

    token, order_id = await _setup(api, make_admin, make_product, make_customer, place_order)
    await _due(db, order_id, next_ring_at=None, stopped_at=text("now()"))
    await StaffOrderAlertService.sweep(db)
    assert not [c for c in fcm.clears if c["data"]["order_id"] == str(order_id)], "not accepted yet"

    await db.execute(update(Order).where(Order.order_id == order_id).values(status="confirmed"))
    await db.commit()
    await StaffOrderAlertService.sweep(db)
    assert len([c for c in fcm.clears if c["data"]["order_id"] == str(order_id)]) == 1
    # Once only.
    await StaffOrderAlertService.sweep(db)
    assert len([c for c in fcm.clears if c["data"]["order_id"] == str(order_id)]) == 1


async def test_a_failed_first_ring_still_rings_later(
    api, db, world, make_admin, make_product, make_customer, place_order, fcm, monkeypatch
):
    from app.services.staff_order_alert_service import StaffOrderAlertService

    real_ring = StaffOrderAlertService._ring

    async def broken(db_, order):
        raise RuntimeError("permission lookup failed")

    monkeypatch.setattr(StaffOrderAlertService, "_ring", staticmethod(broken))
    token, order_id = await _setup(api, make_admin, make_product, make_customer, place_order)
    a = await _alert(db, order_id)
    assert a is not None and a.next_ring_at is not None and a.alert_status == "pending"

    monkeypatch.setattr(StaffOrderAlertService, "_ring", staticmethod(real_ring))
    await _due(db, order_id)
    await StaffOrderAlertService.sweep(db)
    assert len(_for(fcm, order_id)) == 1, "the sweep rang it"


async def test_accepted_while_the_first_ring_was_going_out_stays_stopped(
    api, db, world, make_admin, make_product, make_customer, place_order, fcm, monkeypatch
):
    """Staff confirm (and a sweep clears it) while notify_new_order is still
    sending: its final write must not put the order back in the ring queue."""
    from app.models.admin_push import AdminOrderAlert
    from app.models.order import Order
    from app.services.staff_order_alert_service import StaffOrderAlertService

    real_ring = StaffOrderAlertService._ring
    hit = {}

    async def slow_ring(db_, order):
        result = await real_ring(db_, order)
        if not hit:
            hit["once"] = True
            # Meanwhile, elsewhere: accepted and cleared by a sweep.
            await db.execute(update(Order).where(Order.order_id == order.order_id).values(status="confirmed"))
            await db.execute(
                update(AdminOrderAlert)
                .where(AdminOrderAlert.order_id == order.order_id)
                .values(cleared_at=text("now()"), stopped_at=text("now()"), next_ring_at=None)
            )
            await db.commit()
        return result

    monkeypatch.setattr(StaffOrderAlertService, "_ring", staticmethod(slow_ring))
    token, order_id = await _setup(api, make_admin, make_product, make_customer, place_order)
    a = await _alert(db, order_id)
    assert a.next_ring_at is None and a.stopped_at is not None and a.cleared_at is not None

