"""
App-open coupon popup, collect-many, and the "ends soon" reminder (2026-11-10).

Owner rules pinned here: the popup shows once a day (again only for a coupon
not yet shown), "Shop Now" collects every shown coupon, and the reminder goes
once, only to customers who collected and have not used the coupon, and only
when their Offers switch is on.
"""
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import delete, select

from app.models.coupon import Coupon, CouponExpiryReminder
from app.models.notification import Notification
from app.models.user import User


def _coupon(code: str, *, hours_left: float, popup: bool = False, priority: int = 0) -> Coupon:
    now = datetime.now(timezone.utc)
    return Coupon(
        coupon_id=uuid.uuid4(),
        code=code,
        branch_id=None,
        discount_type="percentage",
        discount_value=Decimal("10"),
        min_order_value=Decimal("0"),
        valid_from=now - timedelta(days=1),
        valid_until=now + timedelta(hours=hours_left),
        is_active=True,
        is_public=True,
        show_in_popup=popup,
        popup_priority=priority,
    )


@pytest.fixture
async def codes(db):
    made: list[str] = []
    yield made
    await db.execute(delete(Coupon).where(Coupon.code.in_(made)))
    await db.commit()


@pytest.mark.asyncio
async def test_popup_shows_once_a_day_and_collects_everything(api, db, make_customer, codes):
    tag = uuid.uuid4().hex[:6].upper()
    big, small, quiet = f"ZZPOPA{tag}", f"ZZPOPB{tag}", f"ZZPOPC{tag}"
    codes += [big, small, quiet]
    db.add_all(
        [
            _coupon(big, hours_left=30, popup=True, priority=5),
            _coupon(small, hours_left=200, popup=True),
            _coupon(quiet, hours_left=200, popup=False),
        ]
    )
    await db.commit()
    me = await make_customer()

    # Guests never get a popup.
    r = await api.get("/api/v1/coupons/spotlight")
    assert r.status_code == 200 and r.json()["data"]["show"] is False

    r = await api.get("/api/v1/coupons/spotlight", headers={**me.headers, "Accept-Language": "si"})
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    shown = [c["code"] for c in data["coupons"]]
    assert data["show"] is True
    assert shown[0] == big, "higher popup priority comes first"
    assert small in shown and quiet not in shown
    assert data["title"] == "ඔබට විශේෂ වට්ටම්!"
    assert data["coupons"][0]["expiry"]["mode"] == "timer"

    ids = [c["coupon_id"] for c in data["coupons"]]
    r = await api.post("/api/v1/coupons/spotlight/seen", json={"coupon_ids": ids}, headers=me.headers)
    assert r.status_code == 200, r.text

    r = await api.get("/api/v1/coupons/spotlight", headers=me.headers)
    assert r.json()["data"]["show"] is False, "once a day"

    # A new popup coupon the same day brings it back.
    fresh = f"ZZPOPD{tag}"
    codes.append(fresh)
    db.add(_coupon(fresh, hours_left=100, popup=True, priority=9))
    await db.commit()
    r = await api.get("/api/v1/coupons/spotlight", headers=me.headers)
    assert r.json()["data"]["show"] is True

    r = await api.post(
        "/api/v1/coupons/collect-many",
        json={"codes": [big, small, "ZZ-NO-SUCH-CODE"]},
        headers=me.headers,
    )
    assert r.status_code == 200, r.text
    assert sorted(r.json()["data"]["collected"]) == sorted([big, small])

    r = await api.post("/api/v1/coupons/evaluate", json={}, headers=me.headers)
    assert r.status_code == 200, r.text
    body = r.json()["data"]
    assert [f["key"] for f in body["filters"]] == ["all", "expiring_soon", "is_new"]
    mine = {c["code"]: c for c in body["coupons"]}
    assert mine[big]["collected"] is True
    assert mine[big]["expiring_soon"] is True and mine[small]["expiring_soon"] is False


@pytest.mark.asyncio
async def test_expiry_reminder_goes_once_to_collectors_with_offers_on(
    api, db, make_customer, codes, monkeypatch
):
    from app.config.redis import get_redis_client
    from app.services import coupon_reminder_worker as worker
    from app.services.fcm_service import FCMService

    async def no_push(cls, **_kw):
        return {"success_count": 0, "invalid_tokens": []}

    monkeypatch.setattr(FCMService, "send_to_tokens", classmethod(no_push))

    tag = uuid.uuid4().hex[:6].upper()
    soon, later = f"ZZREMA{tag}", f"ZZREMB{tag}"
    codes += [soon, later]
    db.add_all([_coupon(soon, hours_left=2), _coupon(later, hours_left=10)])
    await db.commit()

    keen, muted, stranger = await make_customer(), await make_customer(), await make_customer()
    muted_row = await db.get(User, muted.user_id)
    muted_row.notify_offers = False
    await db.commit()

    for who in (keen, muted):
        r = await api.post("/api/v1/coupons/collect-many", json={"codes": [soon, later]}, headers=who.headers)
        assert r.status_code == 200, r.text

    redis = get_redis_client()
    now = datetime.now(timezone.utc)
    assert await worker.remind(db, redis, now) >= 1

    async def notes(user_id):
        rows = await db.execute(
            select(Notification).where(
                Notification.user_id == user_id,
                Notification.data["coupon_code"].astext.in_([soon, later]),
            )
        )
        return list(rows.scalars())

    got = await notes(keen.user_id)
    assert len(got) == 1, "only the coupon inside 3 hours"
    assert got[0].data["screen"] == "coupons"
    assert soon in got[0].message
    assert await notes(muted.user_id) == [], "Offers switch off"
    assert await notes(stranger.user_id) == [], "never collected"

    # A second sweep sends nothing new.
    await worker.remind(db, redis, now)
    assert len(await notes(keen.user_id)) == 1
    claimed = await db.execute(
        select(CouponExpiryReminder).where(CouponExpiryReminder.user_id == keen.user_id)
    )
    assert len(list(claimed.scalars())) == 1
