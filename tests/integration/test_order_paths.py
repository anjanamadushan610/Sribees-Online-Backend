"""
The order paths a happy checkout never takes: stock that fell while the
customer shopped, Buy Now past what is left, reordering a past order at
today's prices, and settling card payments Marx never told us about.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy import select, update

from app.config.redis import RedisKeys


async def _in_branch(app_redis, world, customer):
    from app.services.branch_service import _build_branch_context

    await app_redis.setex(RedisKeys.branch_context(str(customer.user_id)), 600,
                          json.dumps(_build_branch_context(world.branch_id, "ZZ", world.postal_city)))


def _order_body(customer, **extra) -> dict:
    return {"delivery_address_id": str(customer.address_id), "contact_phone": customer.phone,
            "alternate_phone": "+94719999999", "payment_method": "COD", **extra}


async def _set_branch_stock(db, world, pid, stock: int, price: str | None = None):
    from app.models.product import BranchInventory

    values = {"stock_quantity": stock}
    if price is not None:
        values["branch_price"] = Decimal(price)
    await db.execute(update(BranchInventory).where(BranchInventory.branch_id == world.branch_id,
                                                  BranchInventory.product_id == pid).values(**values))
    await db.commit()


async def _cart_qty(api, customer, pid) -> int:
    r = await api.get("/api/v1/cart", headers=customer.headers)
    assert r.status_code == 200, r.text
    items = (r.json().get("data") or {}).get("items") or []
    return sum(int(i.get("quantity", 0)) for i in items if str(i.get("product_id") or i.get("productId")) == str(pid))


async def test_stock_that_fell_while_shopping_updates_the_cart(api, db, app_redis, world, make_customer,
                                                               make_product, fake_courier):
    c = await make_customer()
    pid = await make_product(stock=10)
    await _in_branch(app_redis, world, c)
    r = await api.post("/api/v1/cart/items", json={"product_id": str(pid), "quantity": 3, "price": "1.00", "name": "ZZ"},
                       headers=c.headers)
    assert r.status_code == 200, r.text

    await _set_branch_stock(db, world, pid, 1)
    r = await api.post("/api/v1/orders", json=_order_body(c), headers=c.headers)
    assert r.status_code == 400 and "Only 1 available" in r.text, r.text
    assert await _cart_qty(api, c, pid) == 1, "the cart is corrected to what is left"

    await _set_branch_stock(db, world, pid, 0)
    r = await api.post("/api/v1/orders", json=_order_body(c), headers=c.headers)
    assert r.status_code == 400 and "out of stock" in r.text, r.text
    assert await _cart_qty(api, c, pid) == 0, "a sold-out line leaves the cart"


async def test_buy_now_cannot_take_more_than_is_left(api, db, app_redis, world, make_customer, make_product,
                                                     fake_courier):
    c = await make_customer()
    pid = await make_product(stock=10)
    await _in_branch(app_redis, world, c)
    await _set_branch_stock(db, world, pid, 2)
    body = _order_body(c, is_buy_now=True, items=[{"product_id": str(pid), "quantity": 5}])
    r = await api.post("/api/v1/orders", json=body, headers=c.headers)
    assert r.status_code == 400 and "2 available" in r.text, r.text

    body["items"] = [{"product_id": str(pid), "quantity": 2}]
    r = await api.post("/api/v1/orders", json=body, headers=c.headers)
    assert r.status_code == 201, r.text


async def test_reordering_a_past_order(api, db, app_redis, world, make_customer, make_product, place_order):
    from app.models.product import Product

    c = await make_customer()
    kept, dropped = await make_product(stock=10), await make_product(stock=10)
    order = await place_order(c, kept, 2)
    other = await place_order(c, dropped, 1)
    await api.delete("/api/v1/cart", headers=c.headers)

    # Today's branch price applies, not what they paid last time.
    await _set_branch_stock(db, world, kept, 1, price="99.00")
    r = await api.post(f"/api/v1/orders/{order['order_number']}/reorder", headers=c.headers)
    assert r.status_code == 200, r.text
    assert await _cart_qty(api, c, kept) == 1, "capped at what the branch has"

    await db.execute(update(Product).where(Product.product_id == dropped).values(is_active=False))
    await db.commit()
    r = await api.post(f"/api/v1/orders/{other['order_id']}/reorder", headers=c.headers)
    assert r.status_code == 400, "nothing in it can be bought any more"

    r = await api.post("/api/v1/orders/ZZ-NO-SUCH-ORDER/reorder", headers=c.headers)
    assert r.status_code == 404
    stranger = await make_customer()
    r = await api.post(f"/api/v1/orders/{order['order_id']}/reorder", headers=stranger.headers)
    assert r.status_code in (400, 404), "someone else's order is not theirs to reorder"


async def test_reorder_page_adds_only_what_the_customer_kept(api, db, app_redis, world, make_customer,
                                                            make_product, place_order):
    c = await make_customer()
    tea, rice = await make_product(stock=10), await make_product(stock=10)
    r = await api.post("/api/v1/cart/items", headers=c.headers,
                       json={"product_id": str(tea), "quantity": 3, "price": "1.00", "name": "ZZ item"})
    assert r.status_code == 200, r.text
    order = await place_order(c, rice, 2)  # one order, two lines: tea x3, rice x2
    await api.delete("/api/v1/cart", headers=c.headers)

    await _set_branch_stock(db, world, tea, 2, price="55.00")
    r = await api.get(f"/api/v1/orders/{order['order_number']}/reorder", headers=c.headers)
    assert r.status_code == 200, r.text
    lines = {line["product_id"]: line for line in r.json()["data"]["items"]}
    assert set(lines) == {str(tea), str(rice)}
    assert lines[str(tea)]["ordered_quantity"] == 3
    assert lines[str(tea)]["quantity"] == 2, "the page starts at what can be had"
    assert lines[str(tea)]["unit_price"] == 55.0, "today's price, not last time's"
    assert lines[str(tea)]["available"] is True
    assert await _cart_qty(api, c, tea) == 0, "looking at the page adds nothing"

    # They removed rice and asked for 5 tea; only 2 exist.
    body = {"items": [{"order_item_id": lines[str(tea)]["order_item_id"], "quantity": 5}]}
    r = await api.post(f"/api/v1/orders/{order['order_id']}/reorder", json=body, headers=c.headers)
    assert r.status_code == 200, r.text
    assert await _cart_qty(api, c, tea) == 2
    assert await _cart_qty(api, c, rice) == 0, "a removed line stays out"

    other = await place_order(await make_customer(), rice)
    stray = {"items": [{"order_item_id": str(uuid.uuid4()), "quantity": 1}]}
    r = await api.post(f"/api/v1/orders/{order['order_id']}/reorder", json=stray, headers=c.headers)
    assert r.status_code == 400, "a line from no order of theirs is refused"
    r = await api.get(f"/api/v1/orders/{other['order_id']}/reorder", headers=c.headers)
    assert r.status_code == 404, "someone else's order is not theirs to see"


async def test_card_payments_marx_never_confirmed_are_settled(api, db, world, make_customer, make_product,
                                                              place_order, make_admin, monkeypatch):
    from app.models.order import Order
    from app.services.marx_ipg_service import MarxIPGError, MarxIPGService

    c = await make_customer()
    pid = await make_product(stock=50)
    outcomes = {"SUCCESS": None, "FAILURE": None, "PENDING": None, "ERROR": None}
    for key in outcomes:
        order = await place_order(c, pid)
        tr_id = f"zz-tr-{key}-{uuid.uuid4().hex[:8]}"
        await db.execute(
            update(Order).where(Order.order_id == uuid.UUID(order["order_id"])).values(
                payment_method="MARX_OTHER", payment_status="pending", gateway_tr_id=tr_id,
                created_at=datetime.utcnow() - timedelta(hours=1),
            )
        )
        outcomes[key] = (order["order_id"], tr_id)
    await db.commit()

    async def fake_summary(*, tr_id: str) -> dict:
        if "-ERROR-" in tr_id:
            raise MarxIPGError("gateway down")
        if "-SUCCESS-" in tr_id:
            return {"summary_result": "SUCCESS", "order_status": "CAPTURED", "gateway_code": "00", "raw": {}}
        if "-FAILURE-" in tr_id:
            return {"summary_result": "FAILURE", "gateway_code": "05", "raw": {}}
        return {"summary_result": "PENDING", "raw": {}}

    monkeypatch.setattr(MarxIPGService, "get_order_summary", staticmethod(fake_summary))

    boss = await make_admin("super_admin", api=api)
    r = await api.post("/api/v1/admin/orders/reconcile-payments", headers=boss.headers)
    assert r.status_code == 200, r.text
    assert r.json()["data"]["checked"] >= 4

    db.expire_all()
    status = {}
    for key, (order_id, _) in outcomes.items():
        status[key] = (await db.execute(select(Order.payment_status).where(
            Order.order_id == uuid.UUID(order_id)))).scalar_one()
    assert status == {"SUCCESS": "paid", "FAILURE": "failed", "PENDING": "pending", "ERROR": "pending"}
