"""
First-order nudge (owner, 2026-11-12; docs/plans/first-order-nudge.md).

Pinned here: one push, 48 h after sign-up, at 18:30-21:00 Colombo, only to new
customers with no order and the Offers switch on; free delivery is claimed only
while the branch offer is live, with its minimum; a customer with no served
address is promised nothing; a tap opens Offers; never twice.
"""
import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert

from app.models.first_order_nudge import FirstOrderNudge, FirstOrderNudgeState
from app.models.free_delivery import FreeDeliveryBranchOffer
from app.models.notification import Notification
from app.models.user import Address, User

# 19:00 in Colombo.
NOW = datetime(2026, 11, 20, 13, 30, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def no_push(monkeypatch, app_engine_reset):
    """No real FCM; the push tasks notify() spawns are awaited and the app pool
    disposed after each test (app_engine_reset), so nothing leaks into the next."""
    from app.services.fcm_service import FCMService

    async def _none(cls, **_kw):
        return {"success_count": 0, "invalid_tokens": []}

    monkeypatch.setattr(FCMService, "send_to_tokens", classmethod(_none))


@pytest.fixture
async def since(db, migrated):
    """The feature 'went live' 30 days before NOW."""
    start = NOW - timedelta(days=30)
    await db.execute(
        insert(FirstOrderNudgeState)
        .values(id=1, since=start)
        .on_conflict_do_update(index_elements=["id"], set_={"since": start})
    )
    await db.commit()
    return start


@pytest.fixture
async def offer(db, world):
    """Free delivery on in the test branch: Rs 1,000 minimum, ends 2 Dec 19:16 Colombo."""
    row = FreeDeliveryBranchOffer(
        branch_id=world.branch_id,
        enabled=True,
        months=1,
        min_order=Decimal("1000.00"),
        enabled_at=datetime(2026, 11, 2, 13, 46, tzinfo=timezone.utc),
    )
    db.add(row)
    await db.commit()
    yield row
    await db.execute(delete(FreeDeliveryBranchOffer).where(FreeDeliveryBranchOffer.branch_id == world.branch_id))
    await db.commit()


async def _customer(db, make_customer, *, hours_ago: float = 49, lang: str = "si", name: str = "niluka priyadarshani", **kw):
    who = await make_customer(**kw)
    row = await db.get(User, who.user_id)
    row.created_at = (NOW - timedelta(hours=hours_ago)).replace(tzinfo=None)
    row.preferred_language = lang
    row.full_name = name
    await db.commit()
    return who


async def _notes(db, user_id):
    db.expire_all()
    return (
        await db.execute(
            select(Notification).where(
                Notification.user_id == user_id,
                Notification.type == "promotion",
            )
        )
    ).scalars().all()


async def _sweep(db, now=NOW):
    from app.services import first_order_nudge_worker as worker

    return await worker.nudge(db, now)


@pytest.mark.asyncio
async def test_offer_copy_goes_once_with_name_area_minimum_and_end_date(db, make_customer, world, since, offer):
    who = await _customer(db, make_customer)

    assert await _sweep(db) >= 1
    notes = await _notes(db, who.user_id)
    assert len(notes) == 1
    note = notes[0]
    assert note.message == (
        f"හායි Niluka! 🐝 Sribees Online දැන් {world.postal_city} පැත්තටත් ගෙදරටම බඩු ගෙනත් දෙනවා. "
        "රු. 1,000ට වැඩි ඇණවුම්වලට ඩිලිවරි නොමිලේ, දෙසැම්බර් 2 දක්වා විතරයි. ඔබේ පළමු ඇණවුම අදම දාන්න!"
    )
    assert note.data["screen"] == "offers", "a tap must open the Offers page"
    row = await db.get(FirstOrderNudge, who.user_id)
    assert row is not None and row.variant == "offer_min"

    # Never twice, not even the next evening.
    await _sweep(db)
    await _sweep(db, NOW + timedelta(days=1))
    assert len(await _notes(db, who.user_id)) == 1


@pytest.mark.asyncio
async def test_no_minimum_says_every_order(db, make_customer, since, offer):
    offer.min_order = Decimal("0")
    await db.commit()
    who = await _customer(db, make_customer, lang="en")

    await _sweep(db)
    (note,) = await _notes(db, who.user_id)
    assert "Free delivery on every order, until 2 December only." in note.message
    assert "Rs." not in note.message


@pytest.mark.asyncio
async def test_offer_off_makes_no_free_delivery_claim(db, make_customer, world, since):
    who = await _customer(db, make_customer, lang="en", name="Ashen Madhushanka")

    await _sweep(db)
    (note,) = await _notes(db, who.user_id)
    assert note.message == (
        f"Hi Ashen! 🐝 Sribees Online now delivers to your door in {world.postal_city}. Place your first order today!"
    )
    assert (await db.get(FirstOrderNudge, who.user_id)).variant == "area"


@pytest.mark.asyncio
async def test_expired_offer_makes_no_claim(db, make_customer, since, offer):
    offer.enabled_at = NOW - timedelta(days=40)
    await db.commit()
    who = await _customer(db, make_customer, lang="en")

    await _sweep(db)
    (note,) = await _notes(db, who.user_id)
    assert "Free delivery" not in note.message


@pytest.mark.asyncio
async def test_no_address_gets_the_general_invitation(db, make_customer, since, offer):
    who = await _customer(db, make_customer, with_address=False, lang="ta", name="")

    await _sweep(db)
    (note,) = await _notes(db, who.user_id)
    assert note.message.startswith("வணக்கம் நண்பரே! 🐝 Sribees Online-க்கு வரவேற்கிறோம்.")
    assert "இலவசம்" not in note.message, "no free-delivery promise without a served address"


@pytest.mark.asyncio
async def test_unserved_address_gets_the_general_invitation(db, make_customer, since, offer):
    who = await _customer(db, make_customer, lang="en")
    address = await db.get(Address, who.address_id)
    address.postal_city = "ZZ Nowhere"
    await db.commit()

    await _sweep(db)
    (note,) = await _notes(db, who.user_id)
    assert (await db.get(FirstOrderNudge, who.user_id)).variant == "general"
    assert "ZZ Nowhere" not in note.message and "Free delivery" not in note.message


@pytest.mark.asyncio
async def test_who_is_left_out(db, make_customer, make_product, place_order, since):
    too_soon = await _customer(db, make_customer, hours_ago=47)
    before_launch = await _customer(db, make_customer, hours_ago=24 * 31)
    muted = await _customer(db, make_customer)
    gone = await _customer(db, make_customer)
    blocked = await _customer(db, make_customer)
    ordered = await _customer(db, make_customer)

    (await db.get(User, muted.user_id)).notify_offers = False
    (await db.get(User, gone.user_id)).deleted_at = NOW - timedelta(hours=1)
    (await db.get(User, blocked.user_id)).is_blocked = True
    await db.commit()
    await place_order(ordered, await make_product())

    await _sweep(db)
    for who in (too_soon, before_launch, muted, gone, blocked, ordered):
        assert await _notes(db, who.user_id) == [], who
        assert await db.get(FirstOrderNudge, who.user_id) is None

    # 47 h now becomes 49 h: the next evening it goes.
    await _sweep(db, NOW + timedelta(days=1))
    assert len(await _notes(db, too_soon.user_id)) == 1


@pytest.mark.asyncio
async def test_only_between_six_thirty_and_nine_in_the_evening(db, make_customer, since):
    who = await _customer(db, make_customer, hours_ago=60)
    colombo = timezone(timedelta(hours=5, minutes=30))

    for hour, minute in ((18, 29), (21, 0), (23, 0), (3, 0), (9, 0)):
        moment = NOW.astimezone(colombo).replace(hour=hour, minute=minute)
        assert await _sweep(db, moment) == 0, f"{hour}:{minute:02d}"
    assert await _notes(db, who.user_id) == []

    moment = NOW.astimezone(colombo).replace(hour=18, minute=30)
    assert await _sweep(db, moment) >= 1
    assert len(await _notes(db, who.user_id)) == 1


@pytest.mark.asyncio
async def test_two_workers_at_once_send_one_push(db, db_session_factory, make_customer, since):
    who = await _customer(db, make_customer)
    from app.services import first_order_nudge_worker as worker

    async def one():
        async with db_session_factory() as session:
            return await worker.nudge(session, NOW)

    await asyncio.gather(one(), one())
    assert len(await _notes(db, who.user_id)) == 1


@pytest.mark.asyncio
async def test_first_run_sets_since_to_now_and_keeps_it(db, migrated):
    from app.services import first_order_nudge_worker as worker

    await db.execute(delete(FirstOrderNudgeState))
    await db.commit()
    first = await worker.ensure_since(db, NOW)
    later = await worker.ensure_since(db, NOW + timedelta(days=3))
    assert first == later == NOW


def test_helpers():
    from app.services import first_order_nudge_worker as worker

    end = datetime(2026, 12, 2, 13, 46, tzinfo=timezone.utc)
    assert worker.colombo_date(end, "si") == "දෙසැම්බර් 2"
    assert worker.colombo_date(end, "en") == "2 December"
    assert worker.colombo_date(end, "ta") == "டிசம்பர் 2"
    # 23:00 UTC on 1 Dec is already 2 Dec in Colombo.
    assert worker.colombo_date(datetime(2026, 12, 1, 23, 0, tzinfo=timezone.utc), "en") == "2 December"
    assert worker.rupees(Decimal("1000.00")) == "1,000"
    assert worker.rupees(Decimal("1250.50")) == "1,250.50"
