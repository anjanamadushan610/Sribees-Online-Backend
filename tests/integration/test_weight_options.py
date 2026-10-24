"""
Sold by weight: a product the customer buys as one of its weight options.

The super admin switches a product to sold-by-weight and it starts with
250 g ... 5 kg priced pro rata from its price per kg; each option's price can
be changed, and a branch may set its own. Checkout charges the option's price
for the customer's branch, less the product's Quick Sale discount, ships it at
the option's weight, and freezes the option's name onto the order line.
"""
from __future__ import annotations

import json
import uuid
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.services.weight_options_service import label
from tests.integration.test_admin_catalog import _category, _id


def test_option_labels():
    assert [label(g) for g in (250, 500, 1000, 1500, 2000, 10000)] == [
        "250g", "500g", "1kg", "1.5kg", "2kg", "10kg",
    ]


async def _weighed(api, admin, make_product, price="1000.00"):
    product = await make_product(price=price)
    cat = _id(await _category(api, admin), "category_id", "id")
    r = await api.put(f"/api/v1/admin/products/{product}",
                      json={"sold_by_weight": True, "category_id": cat}, headers=admin.headers)
    assert r.status_code == 200, r.text
    assert r.json()["data"]["sold_by_weight"] is True
    return product


async def _options(api, admin, product, **params):
    r = await api.get(f"/api/v1/admin/products/{product}/weight-options", params=params,
                      headers=admin.headers)
    assert r.status_code == 200, r.text
    return {o["label"]: o for o in r.json()["data"]["options"]}


async def _branch_context(app_redis, world, customer):
    from app.config.redis import RedisKeys
    from app.services.branch_service import _build_branch_context

    await app_redis.setex(
        RedisKeys.branch_context(str(customer.user_id)),
        600,
        json.dumps(_build_branch_context(world.branch_id, "ZZ", world.postal_city)),
    )


async def test_switching_on_starts_with_the_default_options(api, world, make_admin, make_product):
    boss = await make_admin("super_admin", api=api)
    product = await _weighed(api, boss, make_product)

    options = await _options(api, boss, product)
    assert list(options) == ["250g", "500g", "1kg", "2kg", "5kg"]
    assert [options[k]["price"] for k in options] == [250.0, 500.0, 1000.0, 2000.0, 5000.0]

    # Off and on again keeps what the super admin set.
    vid = options["500g"]["variant_id"]
    r = await api.patch(f"/api/v1/admin/products/{product}/weight-options/{vid}",
                        json={"price": "520.00"}, headers=boss.headers)
    assert r.status_code == 200, r.text
    for flag in (False, True):
        r = await api.put(f"/api/v1/admin/products/{product}", json={"sold_by_weight": flag},
                          headers=boss.headers)
        assert r.status_code == 200, r.text
    assert (await _options(api, boss, product))["500g"]["price"] == 520.0


async def test_replacing_the_list_keeps_ids_and_retires_dropped_weights(
    api, world, make_admin, make_product
):
    boss = await make_admin("super_admin", api=api)
    product = await _weighed(api, boss, make_product)
    before = await _options(api, boss, product)

    r = await api.put(
        f"/api/v1/admin/products/{product}/weight-options",
        json={"options": [{"weight_grams": 100, "price": "110"}, {"weight_grams": 1000, "price": "990"}]},
        headers=boss.headers,
    )
    assert r.status_code == 200, r.text
    after = {o["label"]: o for o in r.json()["data"]["options"]}
    assert list(after) == ["100g", "1kg"]
    assert after["1kg"]["variant_id"] == before["1kg"]["variant_id"]
    assert after["1kg"]["price"] == 990.0

    r = await api.put(
        f"/api/v1/admin/products/{product}/weight-options",
        json={"options": [{"weight_grams": 100, "price": "1"}, {"weight_grams": 100, "price": "2"}]},
        headers=boss.headers,
    )
    assert r.status_code == 400


async def test_a_branch_manager_prices_only_their_own_branch(api, world, make_admin, make_product):
    boss = await make_admin("super_admin", api=api)
    product = await _weighed(api, boss, make_product)
    vid = (await _options(api, boss, product))["1kg"]["variant_id"]
    manager = await make_admin("branch_manager", api=api)

    r = await api.put(f"/api/v1/admin/products/{product}/weight-options",
                      json={"options": [{"weight_grams": 500, "price": "1"}]}, headers=manager.headers)
    assert r.status_code == 403
    r = await api.patch(f"/api/v1/admin/products/{product}/weight-options/{vid}",
                        json={"price": "1"}, headers=manager.headers)
    assert r.status_code == 403

    # A branch_id in the body does not move a scoped manager to another branch.
    r = await api.put(f"/api/v1/admin/products/{product}/weight-options/{vid}/branch-price",
                      json={"price": "950", "branch_id": str(uuid.uuid4())}, headers=manager.headers)
    assert r.status_code == 200, r.text
    mine = (await _options(api, manager, product))["1kg"]
    assert mine["branch_price"] == 950.0 and mine["price"] == 950.0 and mine["global_price"] == 1000.0
    assert (await _options(api, boss, product))["1kg"]["branch_price"] is None
    assert (await _options(api, boss, product, branch_id=str(world.branch_id)))["1kg"]["price"] == 950.0

    r = await api.put(f"/api/v1/admin/products/{product}/weight-options/{vid}/branch-price",
                      json={"price": None}, headers=manager.headers)
    assert r.status_code == 200, r.text
    assert (await _options(api, manager, product))["1kg"]["price"] == 1000.0

    r = await api.put(f"/api/v1/admin/products/{product}/weight-options/{vid}/branch-price",
                      json={"price": "900"}, headers=boss.headers)
    assert r.status_code == 400, "a super admin must name the branch"


async def test_the_customer_sees_and_pays_the_branch_price_of_the_chosen_weight(
    api, db, app_redis, world, make_admin, make_product, make_customer, fake_courier
):
    from app.models.order import OrderItem
    from app.models.product import BranchInventory

    boss = await make_admin("super_admin", api=api)
    product = await _weighed(api, boss, make_product)
    options = await _options(api, boss, product)
    half, kilo = options["500g"]["variant_id"], options["1kg"]["variant_id"]
    r = await api.put(f"/api/v1/admin/products/{product}/weight-options/{half}/branch-price",
                      json={"price": "600", "branch_id": str(world.branch_id)}, headers=boss.headers)
    assert r.status_code == 200, r.text
    # A 10 % Quick Sale in the branch takes 10 % off every weight.
    inv = (await db.execute(select(BranchInventory).where(
        BranchInventory.product_id == product, BranchInventory.branch_id == world.branch_id
    ))).scalar_one()
    inv.discount_percentage = 10
    await db.commit()

    customer = await make_customer()
    await _branch_context(app_redis, world, customer)
    r = await api.get(f"/api/v1/products/{product}", headers=customer.headers)
    assert r.status_code == 200, r.text
    shown = {o["label"]: o for o in r.json()["data"]["weight_options"]}
    assert r.json()["data"]["sold_by_weight"] is True
    assert shown["500g"]["original_price"] == 600.0 and shown["500g"]["price"] == 540.0
    assert shown["1kg"]["price"] == 900.0

    for vid, qty in ((half, 2), (kilo, 1)):
        r = await api.post(
            "/api/v1/cart/items",
            json={"product_id": str(product), "variant_id": vid, "quantity": qty,
                  "price": "1.00", "name": "ZZ item"},
            headers=customer.headers,
        )
        assert r.status_code == 200, r.text
    lines = {i["variant_id"]: i for i in r.json()["data"]["items"]}
    assert lines[half]["price"] == 540.0 and lines[half]["variant_name"] == "500g"
    assert lines[kilo]["price"] == 900.0 and lines[kilo]["variant_name"] == "1kg"

    r = await api.post("/api/v1/orders", json={
        "delivery_address_id": str(customer.address_id),
        "contact_phone": customer.phone,
        "alternate_phone": "+94719999999",
        "payment_method": "COD",
    }, headers=customer.headers)
    assert r.status_code == 201, r.text
    order_id = r.json()["data"]["order_id"]

    items = (await db.execute(select(OrderItem).where(OrderItem.order_id == uuid.UUID(order_id)))).scalars().all()
    got = {str(i.variant_id): (i.variant_name, i.quantity, i.unit_price) for i in items}
    assert got == {half: ("500g", 2, Decimal("540.00")), kilo: ("1kg", 1, Decimal("900.00"))}
    # Shipped at the options' weight: 2 x 0.5 kg + 1 x 1 kg.
    weights = [float(q["payload"]["weight_kg"]) for q in fake_courier.quotes.values()]
    assert weights and weights[-1] == pytest.approx(2.0)


async def test_an_unknown_variant_is_charged_as_the_product_and_not_stored(
    api, db, app_redis, world, make_product, make_customer, fake_courier
):
    from app.models.order import OrderItem

    product = await make_product(price="300.00")
    customer = await make_customer()
    await _branch_context(app_redis, world, customer)
    r = await api.post(
        "/api/v1/cart/items",
        json={"product_id": str(product), "variant_id": str(uuid.uuid4()), "quantity": 1,
              "price": "1.00", "name": "ZZ item"},
        headers=customer.headers,
    )
    assert r.status_code == 200, r.text
    r = await api.post("/api/v1/orders", json={
        "delivery_address_id": str(customer.address_id),
        "contact_phone": customer.phone,
        "alternate_phone": "+94719999999",
        "payment_method": "COD",
    }, headers=customer.headers)
    assert r.status_code == 201, r.text
    item = (await db.execute(select(OrderItem).where(
        OrderItem.order_id == uuid.UUID(r.json()["data"]["order_id"])
    ))).scalar_one()
    assert item.variant_id is None and item.variant_name is None
    assert item.unit_price == Decimal("300.00")
