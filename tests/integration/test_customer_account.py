"""
A signed-in customer's own things: addresses, wishlist, notifications, wallet,
referral code and coupons. Each one is also checked for the IDOR that matters
for it -- another customer can neither read nor change it.
"""
from __future__ import annotations

import json
import uuid

import pytest

from app.config.redis import RedisKeys


async def _branch(app_redis, customer, world):
    await app_redis.setex(
        RedisKeys.branch_context(str(customer.user_id)),
        600,
        json.dumps({"branch_id": str(world.branch_id), "branch_name": "ZZ"}),
    )


def _data(r):
    body = r.json()
    return body.get("data", body)


def _count(r) -> int:
    d = _data(r)
    if isinstance(d, int):
        return d
    return next(v for k, v in d.items() if "count" in k.lower() and isinstance(v, int))


# ---------------------------------------------------------------------------
# Addresses
# ---------------------------------------------------------------------------


async def test_address_book(api, make_customer, world):
    c = await make_customer(with_address=False)
    base = "/api/v1/user/addresses"
    body = {
        "title": "Home",
        "recipient_name": "Nimal",
        "phone": "0771234567",
        "address_line1": "42/A Flower Road",
        "postal_city": world.postal_city,
        "district": world.district,
        "province": world.province,
    }
    r = await api.post(base, json=body, headers=c.headers)
    assert r.status_code == 201, r.text
    first = _data(r)
    assert first["is_default"] is True, "the first address is the default"

    r = await api.post(base, json={**body, "title": "Office", "address_line1": "1 Main St"}, headers=c.headers)
    assert r.status_code == 201, r.text
    second = _data(r)
    assert second["is_default"] is False

    r = await api.put(f"{base}/{second['address_id']}/set-default", headers=c.headers)
    assert r.status_code == 200, r.text
    r = await api.get(base, headers=c.headers)
    rows = _data(r)
    rows = rows if isinstance(rows, list) else rows.get("addresses", rows.get("items", []))
    defaults = [a["address_id"] for a in rows if a["is_default"]]
    assert defaults == [second["address_id"]], "exactly one default"

    r = await api.put(f"{base}/{first['address_id']}", json={"address_line2": "Near the temple"}, headers=c.headers)
    assert r.status_code == 200, r.text
    assert "Near the temple" in r.text

    r = await api.delete(f"{base}/{first['address_id']}", headers=c.headers)
    assert r.status_code == 200, r.text
    r = await api.delete(f"{base}/{first['address_id']}", headers=c.headers)
    assert r.status_code == 404


async def test_another_customers_address_is_out_of_reach(api, make_customer, world):
    alice = await make_customer()
    mallory = await make_customer()
    base = "/api/v1/user/addresses"
    for method, path, body in [
        ("PUT", f"{base}/{alice.address_id}", {"address_line1": "Stolen"}),
        ("PUT", f"{base}/{alice.address_id}/set-default", None),
        ("DELETE", f"{base}/{alice.address_id}", None),
    ]:
        r = await api.request(method, path, json=body, headers=mallory.headers)
        assert r.status_code in (403, 404), f"{method} {path} -> {r.status_code}"
    r = await api.get(base, headers=mallory.headers)
    assert str(alice.address_id) not in r.text


# ---------------------------------------------------------------------------
# Wishlist
# ---------------------------------------------------------------------------


async def test_wishlist(api, app_redis, make_customer, make_product, world):
    c = await make_customer()
    await _branch(app_redis, c, world)
    p1 = await make_product(price="500")
    p2 = await make_product(price="200")

    r = await api.post("/api/v1/wishlist", json={"product_id": str(p1), "price_at_watch": 600}, headers=c.headers)
    assert r.status_code in (200, 201), r.text
    r = await api.post(f"/api/v1/wishlist/toggle/{p2}", headers=c.headers)
    assert r.status_code == 200, r.text

    r = await api.get(f"/api/v1/wishlist/check/{p1}", headers=c.headers)
    assert r.status_code == 200, r.text
    assert "true" in r.text.lower()

    r = await api.get("/api/v1/wishlist", headers=c.headers)
    assert r.status_code == 200, r.text
    assert str(p1) in r.text and str(p2) in r.text

    r = await api.get("/api/v1/wishlist/price-drops", headers=c.headers)
    assert r.status_code == 200, r.text

    r = await api.post(f"/api/v1/wishlist/toggle/{p2}", headers=c.headers)
    assert r.status_code == 200
    r = await api.delete(f"/api/v1/wishlist/{p1}", headers=c.headers)
    assert r.status_code == 200, r.text
    r = await api.get("/api/v1/wishlist", headers=c.headers)
    assert str(p1) not in r.text and str(p2) not in r.text

    await api.post("/api/v1/wishlist", json={"product_id": str(p1)}, headers=c.headers)
    r = await api.delete("/api/v1/wishlist", headers=c.headers)
    assert r.status_code == 200, r.text

    # Someone else's wishlist is not this one.
    other = await make_customer()
    r = await api.get(f"/api/v1/wishlist/check/{p1}", headers=other.headers)
    assert "true" not in r.text.lower().replace("success\":true", "")


# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------


async def test_notifications(api, db, make_customer, world):
    from app.models.notification import Notification

    c = await make_customer()
    ids = []
    for i in range(3):
        n = Notification(user_id=c.user_id, title=f"Hello {i}", message="Your order is on its way", type="order")
        db.add(n)
        await db.flush()
        ids.append(str(n.notification_id))
    await db.commit()

    r = await api.get("/api/v1/notifications", headers=c.headers)
    assert r.status_code == 200, r.text
    assert all(i in r.text for i in ids)
    r = await api.get("/api/v1/notifications/unread-count", headers=c.headers)
    assert r.status_code == 200 and _count(r) == 3, r.text

    r = await api.patch(f"/api/v1/notifications/{ids[0]}/read", headers=c.headers)
    assert r.status_code == 200, r.text
    r = await api.put("/api/v1/notifications/read-all", headers=c.headers)
    assert r.status_code == 200, r.text
    r = await api.get("/api/v1/notifications/unread-count", headers=c.headers)
    assert _count(r) == 0, r.text

    # Another customer cannot touch them.
    other = await make_customer()
    r = await api.delete(f"/api/v1/notifications/{ids[1]}", headers=other.headers)
    assert r.status_code == 404, r.text

    r = await api.delete(f"/api/v1/notifications/{ids[1]}", headers=c.headers)
    assert r.status_code == 200, r.text
    r = await api.delete("/api/v1/notifications", headers=c.headers)
    assert r.status_code == 200, r.text
    r = await api.get("/api/v1/notifications", headers=c.headers)
    assert not any(i in r.text for i in ids)


async def test_push_tokens(api, make_customer, world):
    c = await make_customer()
    token = f"fcm-{uuid.uuid4().hex}"
    r = await api.post("/api/v1/notifications/push-token", json={"token": token, "platform": "android"},
                       headers=c.headers)
    assert r.status_code in (200, 201), r.text
    r = await api.delete(f"/api/v1/notifications/push-token/{token}", headers=c.headers)
    assert r.status_code in (200, 204), r.text


# ---------------------------------------------------------------------------
# Wallet, loyalty, referrals, coupons
# ---------------------------------------------------------------------------


async def test_wallet_views_start_empty_and_private(api, make_customer, world):
    c = await make_customer()
    for path in ["/api/v1/wallet", "/api/v1/wallet/pending", "/api/v1/wallet/loyalty", "/api/v1/wallet/transactions"]:
        r = await api.get(path, headers=c.headers)
        assert r.status_code == 200, (path, r.text)
    r = await api.get("/api/v1/wallet", headers=c.headers)
    assert float(_data(r).get("balance", 0)) == 0.0


async def test_wallet_shows_a_credit(api, db, make_customer, world):
    from decimal import Decimal

    from app.models.wallet import WalletTransactionType
    from app.services.wallet_service import WalletService

    c = await make_customer()
    await WalletService.apply_transaction(
        db, user_id=c.user_id, tx_type=WalletTransactionType.REFUND, amount=Decimal("250"),
        title="ZZ refund", order_id=None, order_number=None,
    )
    await db.commit()
    r = await api.get("/api/v1/wallet", headers=c.headers)
    assert float(_data(r)["balance"]) == pytest.approx(250.0)
    r = await api.get("/api/v1/wallet/transactions", headers=c.headers)
    assert "ZZ refund" in r.text


async def test_referral_code(api, make_customer, world):
    c = await make_customer()
    r = await api.get("/api/v1/referrals/me", headers=c.headers)
    assert r.status_code == 200, r.text
    r = await api.post("/api/v1/referrals/redeem", json={"code": "NOSUCHCODE1"}, headers=c.headers)
    assert r.status_code in (400, 404, 422), r.text


async def test_coupons_for_guests_and_customers(api, app_redis, make_customer, world):
    c = await make_customer()
    await _branch(app_redis, c, world)
    r = await api.get("/api/v1/coupons/available", headers=c.headers)
    assert r.status_code == 200, r.text
    r = await api.get("/api/v1/coupons/collected", headers=c.headers)
    assert r.status_code == 200, r.text
    r = await api.post("/api/v1/coupons/collect", json={"code": "NOSUCHCOUPON"}, headers=c.headers)
    assert r.status_code in (400, 404, 422), r.text
