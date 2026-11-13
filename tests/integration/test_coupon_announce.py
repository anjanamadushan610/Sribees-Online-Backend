"""
"A coupon for you" push (owner, 2026-11-13; docs/plans/coupon-announce.md).

Sent when the coupon starts, once per customer, only to the customers it is
for, never to anyone with Offers notifications off, and never as a hidden code
pushed to everyone.
"""
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import delete, select

from app.models.coupon import Coupon, CouponAnnouncement, CouponWhitelistEntry
from app.models.notification import Notification
from app.models.user import User

URL = "/api/v1/admin/coupons"


@pytest.fixture(autouse=True)
def _no_fcm(monkeypatch):
    from app.services.fcm_service import FCMService

    async def no_push(cls, **_kw):
        return {"success_count": 0, "invalid_tokens": []}

    monkeypatch.setattr(FCMService, "send_to_tokens", classmethod(no_push))


@pytest.fixture
async def codes(db):
    made: list[str] = []
    yield made
    await db.execute(delete(Coupon).where(Coupon.code.in_(made)))
    await db.commit()


def _coupon(code, *, target="all", public=True, notify=True, starts_in_h=-1, ends_in_h=48,
            active=True, tier_id=None) -> Coupon:
    now = datetime.now(timezone.utc)
    return Coupon(
        coupon_id=uuid.uuid4(),
        code=code,
        branch_id=None,
        discount_type="fixed",
        discount_value=Decimal("150"),
        min_order_value=Decimal("500"),
        valid_from=now + timedelta(hours=starts_in_h),
        valid_until=now + timedelta(hours=ends_in_h),
        is_active=active,
        is_public=public,
        target_customer_type=target,
        notify_customers=notify,
        min_tier_id=tier_id,
    )


async def _google_user(db) -> User:
    u = User(
        user_id=uuid.uuid4(),
        full_name="ZZ Google Customer",
        email=f"zq.{uuid.uuid4().hex[:8]}@gmail.test",
        auth_provider="google",
        provider_uid=f"google:{uuid.uuid4().hex}",
        is_verified=True,
        is_phone_verified=False,
        is_active=True,
        is_blocked=False,
    )
    db.add(u)
    await db.commit()
    return u


async def _notes(db, user_id, code):
    rows = await db.execute(
        select(Notification).where(
            Notification.user_id == user_id,
            Notification.data["coupon_code"].astext == code,
        )
    )
    return list(rows.scalars())


@pytest.mark.asyncio
async def test_specific_coupon_goes_to_its_list_once_and_later_additions_only(
    api, db, make_customer, codes
):
    from app.services import coupon_announce_worker as worker

    code = f"ZZANNS{uuid.uuid4().hex[:6].upper()}"
    codes.append(code)
    coupon = _coupon(code, target="specific", public=False)
    db.add(coupon)
    await db.flush()

    google = await _google_user(db)                    # listed by account, no number
    by_number = await make_customer()                  # listed by number only
    muted = await make_customer()                      # listed, Offers off
    stranger = await make_customer()                   # not listed
    later = await make_customer()                      # listed after the first sweep
    (await db.get(User, muted.user_id)).notify_offers = False
    db.add_all([
        CouponWhitelistEntry(coupon_id=coupon.coupon_id, phone=None, user_id=google.user_id),
        CouponWhitelistEntry(coupon_id=coupon.coupon_id, phone=by_number.phone, user_id=None),
        CouponWhitelistEntry(coupon_id=coupon.coupon_id, phone=muted.phone, user_id=muted.user_id),
    ])
    await db.commit()

    now = datetime.now(timezone.utc)
    await worker.announce(db, now)
    got = await _notes(db, google.user_id, code)
    assert len(got) == 1 and code in got[0].message and got[0].data["screen"] == "coupons"
    assert len(await _notes(db, by_number.user_id, code)) == 1, "matched by the listed number"
    assert await _notes(db, muted.user_id, code) == [], "Offers switch off"
    assert await _notes(db, stranger.user_id, code) == [], "not on the list"

    # A second sweep sends nothing again; someone added later gets theirs.
    db.add(CouponWhitelistEntry(coupon_id=coupon.coupon_id, phone=later.phone, user_id=later.user_id))
    await db.commit()
    await worker.announce(db, now)
    assert len(await _notes(db, google.user_id, code)) == 1
    assert len(await _notes(db, by_number.user_id, code)) == 1
    assert len(await _notes(db, later.user_id, code)) == 1


@pytest.mark.asyncio
async def test_public_all_coupon_announced_once_when_it_starts(api, db, make_customer, codes):
    from app.services import coupon_announce_worker as worker

    tag = uuid.uuid4().hex[:6].upper()
    live, waiting, off, ended, switched_off, hidden = (f"ZZANN{x}{tag}" for x in "ABCDEF")
    codes += [live, waiting, off, ended, switched_off, hidden]
    db.add_all([
        _coupon(live),
        _coupon(waiting, starts_in_h=5),                  # not started yet
        _coupon(off, notify=False),                       # switch off
        _coupon(ended, starts_in_h=-48, ends_in_h=-1),    # over
        _coupon(switched_off, active=False),              # deactivated
        _coupon(hidden, public=False),                    # hidden code for everyone
    ])
    await db.commit()
    me = await make_customer()

    now = datetime.now(timezone.utc)
    await worker.announce(db, now)
    assert len(await _notes(db, me.user_id, live)) == 1
    for silent in (waiting, off, ended, switched_off, hidden):
        assert await _notes(db, me.user_id, silent) == [], silent

    row = (await db.execute(select(Coupon).where(Coupon.code == live))).scalar_one()
    await db.refresh(row)
    assert row.announced_at is not None, "an All coupon is announced once"

    # Someone who joins after the announcement is not pushed later.
    newcomer = await make_customer()
    await worker.announce(db, now + timedelta(minutes=10))
    assert await _notes(db, newcomer.user_id, live) == []
    assert len(await _notes(db, me.user_id, live)) == 1

    # The waiting coupon goes out once it starts.
    await worker.announce(db, now + timedelta(hours=6))
    assert len(await _notes(db, me.user_id, waiting)) == 1


@pytest.mark.asyncio
async def test_members_only_coupon_reaches_that_level_up(api, db, make_customer, codes):
    from app.models.loyalty import LoyaltyAccount, LoyaltyTier
    from app.services import coupon_announce_worker as worker

    points = 700000 + uuid.uuid4().int % 90000
    tier = LoyaltyTier(name=f"ZZ Annc {points}", min_points=points, is_active=True)
    db.add(tier)
    await db.flush()
    code = f"ZZANNT{uuid.uuid4().hex[:6].upper()}"
    codes.append(code)
    db.add(_coupon(code, target="tier", tier_id=tier.tier_id))
    gold, plain = await make_customer(), await make_customer()
    db.add(LoyaltyAccount(user_id=gold.user_id, lifetime_points=points + 5))
    await db.commit()

    await worker.announce(db, datetime.now(timezone.utc))
    assert len(await _notes(db, gold.user_id, code)) == 1
    assert await _notes(db, plain.user_id, code) == []
    await db.execute(delete(Coupon).where(Coupon.code == code))
    await db.delete(tier)
    await db.commit()


@pytest.mark.asyncio
async def test_not_sent_to_someone_who_cannot_use_it(monkeypatch):
    from app.services import coupon_announce_worker as worker
    from app.services.coupon_service import CouponService

    async def used(db, code, uid):
        return 1

    async def ordered(db, uid, branch_id=None):
        return True

    monkeypatch.setattr(CouponService, "redemptions_by_user", staticmethod(used))
    monkeypatch.setattr(CouponService, "has_completed_order", staticmethod(ordered))
    once = SimpleNamespace(code="X", per_user_limit=1, first_order_only=False, branch_id=None)
    first = SimpleNamespace(code="X", per_user_limit=None, first_order_only=True, branch_id=None)
    free = SimpleNamespace(code="X", per_user_limit=None, first_order_only=False, branch_id=None)
    assert not await worker._can_use(None, once, uuid.uuid4())
    assert not await worker._can_use(None, first, uuid.uuid4())
    assert await worker._can_use(None, free, uuid.uuid4())


@pytest.mark.asyncio
async def test_admin_switch_refuses_a_hidden_code_for_everyone(api, db, world, make_admin, make_customer):
    boss = await make_admin("super_admin", api=api)
    now = datetime.now(timezone.utc)

    def body(**over):
        return {"code": f"ZZN{uuid.uuid4().hex[:8].upper()}", "discount_type": "percentage",
                "discount_value": "10", "valid_from": now.isoformat(),
                "valid_until": (now + timedelta(days=3)).isoformat(), **over}

    r = await api.post(URL, json=body(is_public=False, notify_customers=True), headers=boss.headers)
    assert r.status_code == 400 and "hidden code" in r.text, r.text

    r = await api.post(URL, json=body(is_public=True, notify_customers=True), headers=boss.headers)
    assert r.status_code == 201, r.text
    data = r.json()["data"]
    assert data["notify_customers"] is True and data["notified_count"] == 0
    cid = data["coupon_id"]

    # Hiding it later while the switch is on is refused too.
    r = await api.put(f"{URL}/{cid}", json={"is_public": False}, headers=boss.headers)
    assert r.status_code == 400, r.text

    known = await make_customer()
    r = await api.post(URL, json=body(is_public=False, notify_customers=True, target_customer_type="specific",
                                      whitelisted_customers=[{"phone": known.phone}]),
                       headers=boss.headers)
    assert r.status_code == 201, "a hidden code may go to the customers it was made for"

    # The list shows how many have been told.
    from app.services import coupon_announce_worker as worker

    await worker.announce(db, datetime.now(timezone.utc))
    spec_code = r.json()["data"]["code"]
    r = await api.get(URL, params={"search": spec_code}, headers=boss.headers)
    row = next(c for c in r.json()["data"]["coupons"] if c["code"] == spec_code)
    assert row["notified_count"] == 1
    claimed = await db.execute(
        select(CouponAnnouncement).where(
            CouponAnnouncement.user_id == known.user_id,
            CouponAnnouncement.coupon_id == uuid.UUID(row["coupon_id"]),
        )
    )
    assert len(list(claimed.scalars())) == 1
