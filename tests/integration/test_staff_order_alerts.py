"""
Staff new-order alerts end to end (Sribees New Orders app).

Real database, real checkout, real admin logins; only FCM is replaced: the
fake records every multicast and answers the way FCM does. Owner rules under
test: the order's branch staff with orders:read and every super admin ring;
nobody else; never for an order still awaiting payment; exactly once.
"""
from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import delete, select, update

URL = "/api/v1/admin/push-tokens"


def _token() -> str:
    return f"zz-staff-fcm-{uuid.uuid4().hex}"


async def _settle() -> None:
    """Wait for the background alert tasks the request started."""
    from app.services.notification_service import _background

    for _ in range(50):
        pending = [t for t in list(_background) if not t.done()]
        if not pending:
            return
        await asyncio.wait(pending, timeout=10)


@pytest.fixture
def fcm(monkeypatch):
    """Capture staff multicasts. Set fcm.dead to tokens FCM should call dead."""
    from app.services.fcm_service import FCMService

    class Fake:
        calls: list = []
        dead: set = set()
        raise_error: bool = False

        @staticmethod
        async def send_to_tokens(tokens, title, body, data=None, image_url=None, **kw):
            if Fake.raise_error:
                raise RuntimeError("FCM is down")
            Fake.calls.append({"tokens": list(tokens), "title": title, "body": body, "data": data, **kw})
            dead = [t for t in tokens if t in Fake.dead]
            return {
                "success_count": len(tokens) - len(dead),
                "failure_count": len(dead),
                "failed_tokens": dead,
                "invalid_tokens": dead,
            }

    Fake.calls = []
    Fake.dead = set()
    monkeypatch.setattr(FCMService, "send_to_tokens", Fake.send_to_tokens)
    return Fake


@pytest.fixture(autouse=True)
async def _forget_staff_devices(db):
    """This module's devices must not ring for other modules' orders."""
    yield
    from app.models.admin_push import AdminPushToken

    await db.rollback()
    await db.execute(delete(AdminPushToken).where(AdminPushToken.token.like("zz-staff-fcm-%")))
    await db.commit()


def _rang(fcm, token) -> list:
    """Every staff alert body that went to ``token``."""
    return [c for c in fcm.calls if token in c["tokens"] and c.get("channel_id") == "new_orders"]


async def _register(api, admin, token, device_id=None, language="si"):
    body = {
        "token": token,
        "platform": "android",
        "language": language,
        "device_id": device_id or f"zz-device-{uuid.uuid4().hex[:10]}",
    }
    r = await api.post(URL, json=body, headers=admin.headers)
    assert r.status_code == 200, r.text
    return r.json()["data"]


@pytest.fixture
async def shop(world, make_product, make_customer):
    return await make_product(), await make_customer()


async def test_the_branch_and_super_admins_ring_and_no_one_else(
    api, db, world, second_test_branch, shop, place_order, make_admin, fcm
):
    pid, customer = shop
    here = await make_admin("branch_manager", api=api)
    boss = await make_admin("super_admin", api=api)
    elsewhere = await make_admin("branch_manager", branch_id=second_test_branch.branch_id, api=api)
    t_here, t_boss, t_else = _token(), _token(), _token()
    await _register(api, here, t_here, language="si")
    await _register(api, boss, t_boss, language="en")
    await _register(api, elsewhere, t_else)

    order = await place_order(customer, pid)
    await _settle()

    assert len(_rang(fcm, t_here)) == 1
    assert len(_rang(fcm, t_boss)) == 1
    assert _rang(fcm, t_else) == [], "another branch's manager must not ring"

    si = _rang(fcm, t_here)[0]
    en = _rang(fcm, t_boss)[0]
    number = order["order_number"]
    assert si["title"] == en["title"] == "Sribees New Orders"
    assert f"#{number}" in si["body"] and "නව ඇණවුම" in si["body"] and "භාරදීමේදී ගෙවීම" in si["body"]
    assert en["body"].startswith(f"New order #{number}") and "COD" in en["body"]
    assert si["sound"] == "new_order"
    assert si["data"]["type"] == "new_order"
    assert si["data"]["order_id"] == str(order["order_id"])
    assert si["data"]["branch_id"] == str(world.branch_id)

    from app.models.admin_push import AdminOrderAlert

    latch = await db.get(AdminOrderAlert, uuid.UUID(str(order["order_id"])))
    assert latch is not None and (latch.devices, latch.recipients) == (2, 2)


async def test_a_branch_role_with_orders_read_rings_too(api, shop, place_order, make_admin, fcm):
    """Not only managers: any role of the branch holding orders:read."""
    pid, customer = shop
    support = await make_admin("customer_support", api=api)
    token = _token()
    await _register(api, support, token)
    await place_order(customer, pid)
    await _settle()
    assert len(_rang(fcm, token)) == 1


async def test_no_orders_permission_or_inactive_means_no_ring(
    api, db, world, shop, place_order, make_admin, fcm
):
    from app.models.admin import Admin
    from app.models.admin_push import AdminPushToken

    pid, customer = shop
    marketer = await make_admin("marketing_manager", api=api)
    # The API refuses them (no orders:read) ...
    r = await api.post(URL, json={"token": _token(), "device_id": "zz-x"}, headers=marketer.headers)
    assert r.status_code == 403
    # ... and a row that got there anyway still does not ring.
    t_marketer = _token()
    db.add(AdminPushToken(admin_id=marketer.admin_id, token=t_marketer, device_id="zz-mkt", language="si"))
    # Positive control: alerts do go out for this order.
    boss = await make_admin("super_admin", api=api)
    t_boss = _token()
    await _register(api, boss, t_boss)
    left = await make_admin("branch_manager", api=api)
    t_left = _token()
    await _register(api, left, t_left)
    await db.execute(update(Admin).where(Admin.admin_id == left.admin_id).values(is_active=False))
    await db.commit()

    await place_order(customer, pid)
    await _settle()
    assert len(_rang(fcm, t_boss)) == 1
    assert _rang(fcm, t_marketer) == []
    assert _rang(fcm, t_left) == []


async def test_a_cancelled_order_never_rings(api, db, shop, place_order, make_admin, fcm):
    from app.models.admin_push import AdminOrderAlert
    from app.models.order import Order
    from app.services.staff_order_alert_service import StaffOrderAlertService

    pid, customer = shop
    boss = await make_admin("super_admin", api=api)
    token = _token()
    await _register(api, boss, token)
    placed = await place_order(customer, pid)
    await _settle()
    order_id = uuid.UUID(str(placed["order_id"]))
    await db.execute(delete(AdminOrderAlert).where(AdminOrderAlert.order_id == order_id))
    await db.execute(update(Order).where(Order.order_id == order_id).values(status="cancelled"))
    await db.commit()
    fcm.calls.clear()

    assert await StaffOrderAlertService.notify_new_order(db, order_id) == 0
    assert _rang(fcm, token) == []


async def test_racing_callers_ring_once(api, db, db_session_factory, shop, place_order, make_admin, fcm):
    """A payment callback and the sweep at the same moment: one alert."""
    from app.models.admin_push import AdminOrderAlert
    from app.services.staff_order_alert_service import StaffOrderAlertService

    pid, customer = shop
    boss = await make_admin("super_admin", api=api)
    token = _token()
    await _register(api, boss, token)
    placed = await place_order(customer, pid)
    await _settle()
    order_id = uuid.UUID(str(placed["order_id"]))
    await db.execute(delete(AdminOrderAlert).where(AdminOrderAlert.order_id == order_id))
    await db.commit()
    fcm.calls.clear()

    async def one():
        async with db_session_factory() as s:
            return await StaffOrderAlertService.notify_new_order(s, order_id)

    results = await asyncio.gather(*(one() for _ in range(4)))
    assert sorted(results)[-1] >= 1 and results.count(0) == 3
    assert len(_rang(fcm, token)) == 1


async def test_awaiting_payment_is_silent_then_paid_rings_once(
    api, db, world, shop, place_order, make_admin, fcm
):
    from app.models.admin_push import AdminOrderAlert
    from app.models.order import Order
    from app.schemas.order import PaymentStatusEnum
    from app.services.order_service import OrderService
    from app.services.staff_order_alert_service import StaffOrderAlertService

    pid, customer = shop
    here = await make_admin("branch_manager", api=api)
    token = _token()
    await _register(api, here, token)
    placed = await place_order(customer, pid)
    await _settle()
    order_id = uuid.UUID(str(placed["order_id"]))

    # Make it a card order that has not been paid yet, alert not yet sent.
    await db.execute(delete(AdminOrderAlert).where(AdminOrderAlert.order_id == order_id))
    await db.execute(update(Order).where(Order.order_id == order_id).values(status="awaiting_payment"))
    await db.commit()
    fcm.calls.clear()

    assert await StaffOrderAlertService.notify_new_order(db, order_id) == 0
    assert _rang(fcm, token) == []
    assert await db.get(AdminOrderAlert, order_id) is None, "an unpaid order must not take the latch"

    db.expire_all()
    order = await db.get(Order, order_id)
    await OrderService.update_payment_status(db, order, PaymentStatusEnum.PAID, payment_id="zz-pay")
    await _settle()
    assert len(_rang(fcm, token)) == 1

    # A retried callback / sweep reaching the hook again rings nobody.
    assert await StaffOrderAlertService.notify_new_order(db, order_id) == 0
    await _settle()
    assert len(_rang(fcm, token)) == 1


async def test_fcm_failing_never_costs_the_order(api, db, shop, place_order, make_admin, fcm):
    pid, customer = shop
    here = await make_admin("branch_manager", api=api)
    await _register(api, here, _token())
    fcm.raise_error = True
    try:
        placed = await place_order(customer, pid)  # asserts 201 inside
        await _settle()
    finally:
        fcm.raise_error = False

    from app.models.order import Order

    assert await db.get(Order, uuid.UUID(str(placed["order_id"]))) is not None


async def test_a_dead_token_stops_being_used(api, db, shop, place_order, make_admin, fcm):
    from app.models.admin_push import AdminPushToken

    pid, customer = shop
    here = await make_admin("branch_manager", api=api)
    token = _token()
    await _register(api, here, token)
    fcm.dead = {token}
    await place_order(customer, pid)
    await _settle()

    db.expire_all()
    row = (await db.execute(select(AdminPushToken).where(AdminPushToken.token == token))).scalar_one()
    assert row.is_active is False


async def test_a_shared_phone_follows_whoever_signed_in_and_logout_stops_it(
    api, db, shop, place_order, make_admin, fcm
):
    from app.models.admin_push import AdminPushToken

    pid, customer = shop
    first = await make_admin("branch_manager", api=api)
    second = await make_admin("branch_manager", api=api)
    device = f"zz-device-{uuid.uuid4().hex[:8]}"
    old, new = _token(), _token()

    await _register(api, first, old, device)
    # The same install signs in as someone else and FCM has rotated the token.
    moved = await _register(api, second, new, device, language="en")
    assert moved["device_id"] == device and moved["language"] == "en"

    db.expire_all()
    rows = (await db.execute(select(AdminPushToken).where(AdminPushToken.device_id == device))).scalars().all()
    assert [(r.token, r.admin_id) for r in rows] == [(new, second.admin_id)]

    # Same token re-registered by the first admin: re-bound, not duplicated.
    await _register(api, first, new, device)
    db.expire_all()
    rows = (await db.execute(select(AdminPushToken).where(AdminPushToken.token == new))).scalars().all()
    assert len(rows) == 1 and rows[0].admin_id == first.admin_id

    r = await api.delete(f"{URL}/{device}", headers=first.headers)
    assert r.status_code == 200 and r.json()["data"]["deactivated"] == 1
    # Twice is fine.
    r = await api.delete(f"{URL}/{device}", headers=first.headers)
    assert r.status_code == 200

    boss = await make_admin("super_admin", api=api)
    t_boss = _token()
    await _register(api, boss, t_boss)
    await place_order(customer, pid)
    await _settle()
    assert len(_rang(fcm, t_boss)) == 1, "positive control: the order did ring"
    assert _rang(fcm, new) == [], "a logged-out phone must stay quiet"


async def test_the_routes_need_a_login(api):
    assert (await api.post(URL, json={"token": "x", "device_id": "d"})).status_code in (401, 403)
    assert (await api.delete(f"{URL}/x")).status_code in (401, 403)


async def test_a_device_id_is_required(api, make_admin):
    """Logout works by device: a row without one could never be switched off."""
    boss = await make_admin("super_admin", api=api)
    r = await api.post(URL, json={"token": _token()}, headers=boss.headers)
    assert r.status_code == 422
