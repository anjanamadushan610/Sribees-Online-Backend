"""
POST /coupons/evaluate, and the coupon block on the checkout quote, over HTTP
against the real app.

The app draws its coupon screens from these responses and decides nothing
itself, so what is pinned here is what a customer sees: the section a card is
in, its button, its words in their language, and a discount that is the one
checkout charges.
"""
import json
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.config.redis import RedisKeys
from app.models.coupon import Coupon


async def _coupon(db, code, **over):
    now = datetime.now(timezone.utc)
    row = Coupon(
        coupon_id=uuid.uuid4(),
        code=code,
        description=f"{code} test offer",
        discount_type="percentage",
        discount_value=Decimal("10"),
        min_order_value=Decimal("0"),
        valid_from=now - timedelta(days=1),
        valid_until=now + timedelta(days=10),
        is_active=True,
        is_public=True,
    )
    for key, value in over.items():
        setattr(row, key, value)
    db.add(row)
    await db.commit()
    return row


async def _at_branch(app_redis, world, customer):
    from app.services.branch_service import _build_branch_context

    await app_redis.setex(
        RedisKeys.branch_context(str(customer.user_id)),
        600,
        json.dumps(_build_branch_context(world.branch_id, "ZZ", world.postal_city)),
    )


@pytest.fixture
async def shop(db, api, app_redis, world, make_product, make_customer):
    """A customer at the test branch, a Rs 1,000 product, and three offers."""
    customer = await make_customer()
    await _at_branch(app_redis, world, customer)
    product = await make_product(price="1000.00")
    s = world.suffix
    codes = {
        "ten": f"TEN{s}",           # 10% off, no minimum
        "big": f"BIG{s}",           # needs Rs 5,000
        "flat": f"FLAT{s}",         # Rs 150 off from Rs 1,500
    }
    await _coupon(db, codes["ten"])
    await _coupon(db, codes["big"], min_order_value=Decimal("5000"))
    await _coupon(
        db,
        codes["flat"],
        discount_type="fixed",
        discount_value=Decimal("150"),
        min_order_value=Decimal("1500"),
    )
    return customer, product, codes


def _by_code(data):
    return {c["code"]: c for c in data["coupons"]}


@pytest.mark.asyncio
async def test_a_basket_sorts_every_offer_and_prices_it_like_checkout(api, shop):
    customer, product, codes = shop
    r = await api.post(
        "/api/v1/coupons/evaluate",
        json={"items": [{"product_id": str(product), "quantity": 2}]},
        headers={**customer.headers, "Accept-Language": "si-LK"},
    )
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    cards = _by_code(data)
    assert data["basket"] is True

    ten, big, flat = cards[codes["ten"]], cards[codes["big"]], cards[codes["flat"]]
    assert (ten["section"], ten["action"], ten["estimated_discount"]) == ("eligible", "use", 200.0)
    assert ten["label"] == "10% වට්ටම්"
    assert flat["estimated_discount"] == 150.0
    assert (big["section"], big["action"]) == ("locked", "collect")
    assert big["reason"] == "මෙම දීමනාව ලබා ගැනීමට තව රු. 3,000ක් එකතු කරන්න."
    assert "අවම ඇණවුම රු. 5,000" in big["terms"]
    # Biggest saving first, and sections in display order with their titles.
    eligible = [c["code"] for c in data["coupons"] if c["section"] == "eligible"]
    assert eligible.index(codes["ten"]) < eligible.index(codes["flat"])
    assert data["sections"][0] == {"key": "eligible", "title": "ලබා ගත හැකි දීමනා"}


@pytest.mark.asyncio
async def test_the_client_price_is_ignored(api, shop):
    """Only product and quantity are read; a doctored price changes nothing."""
    customer, product, codes = shop
    r = await api.post(
        "/api/v1/coupons/evaluate",
        json={"items": [{"product_id": str(product), "quantity": 1, "price": 999999}]},
        headers=customer.headers,
    )
    assert r.status_code == 200, r.text
    assert _by_code(r.json()["data"])[codes["ten"]]["estimated_discount"] == 100.0


@pytest.mark.asyncio
async def test_the_cart_line_needs_only_the_applied_coupon(api, shop):
    customer, product, codes = shop
    r = await api.post(
        "/api/v1/coupons/evaluate",
        json={
            "items": [{"product_id": str(product), "quantity": 1}],
            "applied_code": codes["flat"].lower(),
            "include_offers": False,
        },
        headers={**customer.headers, "Accept-Language": "ta"},
    )
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["coupons"] == []
    applied = data["applied"]
    assert (applied["code"], applied["eligible"], applied["discount"]) == (codes["flat"], False, 0.0)
    assert applied["reason"] == "இந்த சலுகையைப் பெற மேலும் ரூ. 500 சேர்க்கவும்."


@pytest.mark.asyncio
async def test_an_unknown_applied_code_says_so(api, shop):
    customer, product, _ = shop
    r = await api.post(
        "/api/v1/coupons/evaluate",
        json={"items": [{"product_id": str(product)}], "applied_code": "NOPE-NOT-A-CODE"},
        headers=customer.headers,
    )
    assert r.json()["data"]["applied"]["reason"] == "Invalid coupon code."


@pytest.mark.asyncio
async def test_nothing_ticked_shows_the_offers_and_asks_for_a_selection(api, shop):
    customer, _, codes = shop
    r = await api.post("/api/v1/coupons/evaluate", json={"items": []}, headers=customer.headers)
    data = r.json()["data"]
    assert data["basket"] is False
    assert data["notice"] == "Select items in your cart to see which coupons apply."
    assert _by_code(data)[codes["ten"]]["section"] == "collect"


@pytest.mark.asyncio
async def test_collecting_moves_an_offer_into_my_coupons(api, shop):
    customer, _, codes = shop
    r = await api.post("/api/v1/coupons/collect", json={"code": codes["ten"]}, headers=customer.headers)
    assert r.status_code == 200, r.text
    r = await api.post("/api/v1/coupons/evaluate", json={}, headers=customer.headers)
    card = _by_code(r.json()["data"])[codes["ten"]]
    assert (card["section"], card["action"], card["status"]["text"]) == ("wallet", None, "READY")


@pytest.mark.asyncio
async def test_the_whole_cart_can_be_evaluated(api, shop):
    customer, product, codes = shop
    r = await api.post(
        "/api/v1/cart/items",
        json={"product_id": str(product), "quantity": 3, "price": "1.00", "name": "ZZ item"},
        headers=customer.headers,
    )
    assert r.status_code == 200, r.text
    r = await api.post("/api/v1/coupons/evaluate", json={"use_cart": True}, headers=customer.headers)
    assert _by_code(r.json()["data"])[codes["ten"]]["estimated_discount"] == 300.0


@pytest.mark.asyncio
async def test_a_guest_can_browse_but_not_price_a_basket(api, shop):
    _, product, codes = shop
    r = await api.post("/api/v1/coupons/evaluate", json={})
    assert r.status_code == 200, r.text
    assert r.json()["data"]["basket"] is False
    r = await api.post(
        "/api/v1/coupons/evaluate", json={"items": [{"product_id": str(product)}]}
    )
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_the_quote_says_why_a_coupon_did_not_apply(api, shop, fake_courier):
    customer, product, codes = shop
    r = await api.post(
        "/api/v1/cart/items",
        json={"product_id": str(product), "quantity": 1, "price": "1.00", "name": "ZZ item"},
        headers=customer.headers,
    )
    assert r.status_code == 200, r.text

    r = await api.post(
        "/api/v1/orders/quote",
        json={"delivery_address_id": str(customer.address_id), "coupon_code": codes["big"]},
        headers={**customer.headers, "Accept-Language": "en"},
    )
    assert r.status_code == 200, r.text
    coupon = r.json()["data"]["coupon"]
    assert coupon["applied"] is False
    assert coupon["discount"] == 0.0
    assert coupon["reason"] == "Add Rs. 4,000 more to unlock this offer."

    r = await api.post(
        "/api/v1/orders/quote",
        json={"delivery_address_id": str(customer.address_id), "coupon_code": codes["ten"]},
        headers=customer.headers,
    )
    coupon = r.json()["data"]["coupon"]
    assert (coupon["applied"], coupon["discount"], coupon["reason"]) == (True, 100.0, None)
    assert coupon["label"] == "10% OFF"

    r = await api.post(
        "/api/v1/orders/quote",
        json={"delivery_address_id": str(customer.address_id)},
        headers=customer.headers,
    )
    assert r.json()["data"]["coupon"] is None


@pytest.mark.asyncio
async def test_applying_a_coupon_refuses_in_the_customers_language(api, shop):
    customer, product, codes = shop
    await api.post(
        "/api/v1/cart/items",
        json={"product_id": str(product), "quantity": 1, "price": "1.00", "name": "ZZ item"},
        headers=customer.headers,
    )
    r = await api.post(
        "/api/v1/cart/coupon",
        json={"code": codes["big"]},
        headers={**customer.headers, "Accept-Language": "ta"},
    )
    assert r.status_code == 400
    assert "ரூ. 4,000" in r.text

    r = await api.post(
        "/api/v1/cart/coupon", json={"code": "NOPE-NOT-A-CODE"}, headers=customer.headers
    )
    assert r.status_code == 400
    assert "Invalid coupon code." in r.text
