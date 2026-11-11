"""
Offer loss caps end to end (docs/plans/offer-loss-cap.md): the admin makes an
offer, prices go on, the cart and checkout stop a customer at the hidden cap,
a cancel gives the room back, and the end puts prices back exactly.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.config.redis import RedisKeys

SI = {"Accept-Language": "si"}


async def _set_branch(app_redis, customer, world):
    await app_redis.setex(
        RedisKeys.branch_context(str(customer.user_id)),
        600,
        json.dumps({"branch_id": str(world.branch_id), "branch_name": "ZZ"}),
    )


async def _add(api, customer, product_id, qty=1, lang=None):
    return await api.post(
        "/api/v1/cart/items",
        json={"product_id": str(product_id), "quantity": qty, "price": "1.00", "name": "ZZ"},
        headers={**customer.headers, **(lang or {})},
    )


def _order(customer):
    return {
        "delivery_address_id": str(customer.address_id),
        "contact_phone": customer.phone,
        "alternate_phone": "+94719999999",
        "payment_method": "COD",
    }


async def _offer(api, admin, world, items, cap="1000", starts=None, ends=None):
    now = datetime.now(timezone.utc)
    r = await api.post(
        "/api/v1/admin/offer-caps",
        json={
            "branch_id": str(world.branch_id),
            "name": "ZZ 11.11",
            "cap_amount": cap,
            "starts_at": (starts or now - timedelta(minutes=1)).isoformat(),
            "ends_at": (ends or now + timedelta(days=1)).isoformat(),
            "items": [{"product_id": str(p), "offer_price": price} for p, price in items],
        },
        headers=admin.headers,
    )
    assert r.status_code == 200, r.text
    return r.json()["data"]


async def _row(db, product, world):
    from app.models.product import BranchInventory

    db.expire_all()
    return (
        await db.execute(
            select(BranchInventory).where(
                BranchInventory.product_id == product, BranchInventory.branch_id == world.branch_id
            )
        )
    ).scalar_one()


@pytest.fixture
async def offer_world(api, db, world, make_product, make_admin):
    sugar = await make_product(price="260.00", stock=100)
    salmon = await make_product(price="560.00", stock=100)
    other = await make_product(price="300.00", stock=100)
    old_sale = await make_product(price="290.00", stock=100, discount=10.344828)
    admin = await make_admin("super_admin", api=api)
    offer = await _offer(api, admin, world, [(sugar, "199.00"), (salmon, "400.00")])
    return {"sugar": sugar, "salmon": salmon, "other": other, "old_sale": old_sale, "admin": admin, "offer": offer}


async def test_prices_go_on_and_come_back_exactly(api, db, world, offer_world):
    w = offer_world
    assert w["offer"]["status"] == "live"
    row = await _row(db, w["sugar"], world)
    assert row.is_on_sale and Decimal(str(row.branch_price)) == Decimal("260.00")
    assert round(260 * (1 - row.discount_percentage / 100), 2) == 199.00

    r = await api.post(f"/api/v1/admin/offer-caps/{w['offer']['offer_id']}/end", headers=w["admin"].headers)
    assert r.status_code == 200 and r.json()["data"]["status"] == "ended"
    row = await _row(db, w["sugar"], world)
    assert row.branch_price is None and row.discount_percentage is None and row.is_on_sale is False


async def test_sixteen_kilos_fit_the_seventeenth_does_not(api, db, app_redis, world, make_customer, offer_world, fake_courier):
    w = offer_world
    customer = await make_customer()
    await _set_branch(app_redis, customer, world)

    r = await _add(api, customer, w["sugar"], 16)  # 16 x 61 = 976
    assert r.status_code == 200, r.text
    cart = r.json()["data"]
    assert cart["offer_limit"]["reached"] is True  # 976 + 61 > 1000: no room for one more
    assert "1000" not in (cart["offer_limit"]["message"] or "") and "1,000" not in cart["offer_limit"]["message"]

    r = await _add(api, customer, w["sugar"], 1, SI)
    assert r.status_code == 400
    assert "offer සීමාව" in r.json()["detail"]
    r = await api.put(f"/api/v1/cart/items/{w['sugar']}", json={"quantity": 17}, headers=customer.headers)
    assert r.status_code == 400

    # Things outside the offer, and older Quick Sale items, are never limited.
    assert (await _add(api, customer, w["other"], 5)).status_code == 200
    assert (await _add(api, customer, w["old_sale"], 20)).status_code == 200
    # Lowering is always allowed.
    r = await api.put(f"/api/v1/cart/items/{w['sugar']}", json={"quantity": 10}, headers=customer.headers)
    assert r.status_code == 200, r.text


async def test_the_room_is_shared_and_spans_orders_and_a_cancel_gives_it_back(
    api, db, app_redis, world, make_customer, offer_world, fake_courier
):
    from app.models.offer_cap import OfferCapUsage

    w = offer_world
    customer = await make_customer()
    await _set_branch(app_redis, customer, world)

    assert (await _add(api, customer, w["salmon"], 1)).status_code == 200  # 160
    assert (await _add(api, customer, w["sugar"], 13)).status_code == 200  # +793 = 953
    assert (await _add(api, customer, w["sugar"], 1)).status_code == 400  # 1014
    r = await api.post("/api/v1/orders", json=_order(customer), headers=customer.headers)
    assert r.status_code == 201, r.text
    first = r.json()["data"]
    usage = (await db.execute(select(OfferCapUsage.amount).where(OfferCapUsage.user_id == customer.user_id))).scalars().all()
    assert [Decimal(str(u)) for u in usage] == [Decimal("953.00")]

    # The next order sees the first: 47 left, a sugar (61) does not fit.
    assert (await _add(api, customer, w["sugar"], 1)).status_code == 400

    r = await api.post(f"/api/v1/orders/{first['order_id']}/cancel?reason=x", headers=customer.headers)
    assert r.status_code == 200, r.text
    assert (await _add(api, customer, w["sugar"], 16)).status_code == 200


async def test_checkout_refuses_a_cart_pushed_over_by_sync(api, db, app_redis, world, make_customer, offer_world, fake_courier):
    w = offer_world
    customer = await make_customer()
    await _set_branch(app_redis, customer, world)
    # An old app's sync writes the cart without the add-time check.
    r = await api.post(
        "/api/v1/cart/sync",
        json={"items": [{"product_id": str(w["sugar"]), "quantity": 30, "price": 1, "name": "x"}], "updated_at": 9999999999},
        headers=customer.headers,
    )
    assert r.status_code == 200, r.text
    assert r.json()["data"]["offer_limit"]["reached"] is True
    r = await api.post("/api/v1/orders", json=_order(customer), headers=customer.headers)
    assert r.status_code == 400, r.text


async def test_another_customer_has_their_own_room(api, db, app_redis, world, make_customer, offer_world, fake_courier):
    w = offer_world
    a, b = await make_customer(), await make_customer()
    for c in (a, b):
        await _set_branch(app_redis, c, world)
        assert (await _add(api, c, w["sugar"], 16)).status_code == 200


async def test_an_offer_not_started_limits_nothing(api, db, app_redis, world, make_customer, make_product, make_admin, fake_courier):
    product = await make_product(price="270.00", stock=100)
    admin = await make_admin("super_admin", api=api)
    now = datetime.now(timezone.utc)
    offer = await _offer(api, admin, world, [(product, "198.00")], cap="100",
                         starts=now + timedelta(days=1), ends=now + timedelta(days=2))
    assert offer["status"] == "scheduled"
    customer = await make_customer()
    await _set_branch(app_redis, customer, world)
    assert (await _add(api, customer, product, 10)).status_code == 200
    row = await _row(db, product, world)
    assert not row.is_on_sale


async def test_a_product_cannot_sit_in_two_offers_at_once(api, world, offer_world):
    w = offer_world
    now = datetime.now(timezone.utc)
    r = await api.post(
        "/api/v1/admin/offer-caps",
        json={
            "branch_id": str(world.branch_id), "name": "dup", "cap_amount": "500",
            "starts_at": now.isoformat(), "ends_at": (now + timedelta(hours=2)).isoformat(),
            "items": [{"product_id": str(w["sugar"]), "offer_price": "150.00"}],
        },
        headers=w["admin"].headers,
    )
    assert r.status_code == 409


async def test_the_cap_holds_until_the_prices_are_restored(api, db, app_redis, world, make_customer, offer_world, fake_courier):
    """Past ends_at but before the sweep restores: still offer prices, so still capped."""
    from sqlalchemy import update

    from app.models.offer_cap import OfferCap

    w = offer_world
    await db.execute(
        update(OfferCap)
        .where(OfferCap.offer_id == w["offer"]["offer_id"])
        .values(starts_at=datetime.now(timezone.utc) - timedelta(hours=2), ends_at=datetime.now(timezone.utc) - timedelta(minutes=1))
    )
    await db.commit()
    customer = await make_customer()
    await _set_branch(app_redis, customer, world)
    assert (await _add(api, customer, w["sugar"], 17)).status_code == 400


async def test_a_broken_offer_is_cancelled_and_never_blocks_an_end(api, db, world, make_product, offer_world):
    from sqlalchemy import update

    from app.models.offer_cap import OfferCap, OfferCapItem
    from app.services import offer_cap_service

    w = offer_world
    # A scheduled offer whose price is no longer below normal (the shelf price fell).
    cheap = await make_product(price="100.00", stock=5)
    now = datetime.now(timezone.utc)
    broken = OfferCap(branch_id=world.branch_id, name="broken", cap_amount=Decimal("100"),
                      starts_at=now - timedelta(minutes=5), ends_at=now + timedelta(hours=1), status="scheduled")
    db.add(broken)
    await db.flush()
    db.add(OfferCapItem(offer_id=broken.offer_id, product_id=cheap, offer_price=Decimal("150.00")))
    # And the live offer is due to end.
    await db.execute(update(OfferCap).where(OfferCap.offer_id == w["offer"]["offer_id"])
                     .values(starts_at=now - timedelta(hours=2), ends_at=now - timedelta(minutes=1)))
    await db.commit()
    # A manager re-priced salmon during the offer: their price must stand.
    from app.models.product import BranchInventory
    await db.execute(update(BranchInventory).where(BranchInventory.product_id == w["salmon"], BranchInventory.branch_id == world.branch_id)
                     .values(branch_price=Decimal("550.00"), discount_percentage=None, is_on_sale=False))
    await db.commit()

    await offer_cap_service.apply_due(db)
    db.expire_all()
    statuses = {o.name: o.status for o in (await db.execute(select(OfferCap).where(OfferCap.branch_id == world.branch_id))).scalars()}
    assert statuses["broken"] == "cancelled" and statuses["ZZ 11.11"] == "ended"
    sugar = await _row(db, w["sugar"], world)
    assert sugar.discount_percentage is None and sugar.is_on_sale is False
    salmon = await _row(db, w["salmon"], world)
    assert Decimal(str(salmon.branch_price)) == Decimal("550.00")
