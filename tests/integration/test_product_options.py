"""
Product options: one product in several shades / colours (migration 123,
app/services/product_options.py).

The super admin gives a product an option label ("Shade") and its options,
each with its own photo. An option costs the product's price unless it has one
of its own; the product's Quick Sale discount applies either way. Each shade
is counted on its own in a branch. Checkout takes the chosen shade's stock and
freezes its name on the order line; a line without a shade -- what an old app
sends -- is refused with OPTION_REQUIRED.
"""
from __future__ import annotations

import asyncio
import uuid
from decimal import Decimal

from sqlalchemy import select

from app.services import stock_keeping
from tests.integration.test_weight_options import _branch_context, _inventory_id

ORDER = {"alternate_phone": "+94719999999", "payment_method": "COD"}


async def _shades(api, boss, product, options, label="Shade"):
    r = await api.put(f"/api/v1/admin/products/{product}/options",
                      json={"option_label": label, "options": options}, headers=boss.headers)
    assert r.status_code == 200, r.text
    return {o["name"]: o for o in r.json()["data"]["options"]}


async def _count(api, manager, inventory_id, packs: dict):
    r = await api.put(f"/api/v1/admin/inventory/{inventory_id}/weights", headers=manager.headers, json={
        "packs": [{"variant_id": vid, "stock_quantity": n} for vid, n in packs.items()]})
    assert r.status_code == 200, r.text
    return r.json()["data"]


async def _add(api, customer, product, variant_id=None, qty=1):
    body = {"product_id": str(product), "quantity": qty, "price": "1.00", "name": "ZZ polish"}
    if variant_id:
        body["variant_id"] = variant_id
    return await api.post("/api/v1/cart/items", json=body, headers=customer.headers)


async def _checkout(api, customer):
    return await api.post("/api/v1/orders", json={
        "delivery_address_id": str(customer.address_id), "contact_phone": customer.phone, **ORDER,
    }, headers=customer.headers)


async def _polish(api, boss, db, world, make_product, *, price="400.00"):
    product = await make_product(price=price)
    shades = await _shades(api, boss, product, [
        {"name": "Shade 07", "image_url": "products/zz-07.webp"},
        {"name": "Shade 40", "image_url": "products/zz-40.webp"},
        {"name": "Shade 68", "price": "450.00"},
    ])
    return product, shades, await _inventory_id(db, world, product)


async def test_shades_have_own_photo_price_and_stock(
    api, db, app_redis, world, make_admin, make_product, make_customer
):
    from app.models.product import BranchInventory

    boss = await make_admin("super_admin", api=api)
    manager = await make_admin("branch_manager", api=api)
    product, shades, inventory_id = await _polish(api, boss, db, world, make_product)
    assert list(shades) == ["Shade 07", "Shade 40", "Shade 68"]
    assert shades["Shade 07"]["price"] is None and shades["Shade 68"]["price"] == 450.0

    counted = await _count(api, manager, inventory_id, {
        shades["Shade 07"]["variant_id"]: 5, shades["Shade 40"]["variant_id"]: 0,
        shades["Shade 68"]["variant_id"]: 2})
    assert counted["option_label"] == "Shade"
    assert {o["label"]: o["packs"] for o in counted["options"]} == {"Shade 07": 5, "Shade 40": 0, "Shade 68": 2}
    assert counted["stock_quantity"] == 7, "all shades together, so lists show it in stock"

    # A 10 % Quick Sale in the branch and a branch price of its own.
    inv = (await db.execute(select(BranchInventory).where(
        BranchInventory.inventory_id == inventory_id))).scalar_one()
    inv.discount_percentage = 10
    inv.branch_price = Decimal("420.00")
    await db.commit()

    customer = await make_customer()
    await _branch_context(app_redis, world, customer)
    r = await api.get(f"/api/v1/products/{product}", headers=customer.headers)
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["option_label"] == "Shade"
    shown = {o["name"]: o for o in data["options"]}
    assert list(shown) == ["Shade 07", "Shade 40", "Shade 68"]
    # Follows the product: the branch's 420 less 10 %.
    assert shown["Shade 07"]["original_price"] == 420.0 and shown["Shade 07"]["price"] == 378.0
    assert shown["Shade 07"]["image_url"].endswith("products/zz-07.webp")
    assert shown["Shade 68"]["image_url"] is None, "no photo of its own: the app shows the product's"
    # Its own price is that price (not scaled by the branch's 420/400), less 10 %.
    assert shown["Shade 68"]["original_price"] == 450.0 and shown["Shade 68"]["price"] == 405.0
    assert shown["Shade 07"]["available"] == 5 and shown["Shade 40"]["available"] == 0

    # The cart charges the same numbers.
    r = await _add(api, customer, product, shades["Shade 07"]["variant_id"], 2)
    assert r.status_code == 200, r.text
    line = r.json()["data"]["items"][0]
    assert line["price"] == 378.0 and line["variant_name"] == "Shade 07"


async def test_checkout_takes_the_chosen_shade_and_a_cancel_gives_it_back(
    api, db, app_redis, world, make_admin, make_product, make_customer, fake_courier
):
    from app.models.order import OrderItem

    boss = await make_admin("super_admin", api=api)
    manager = await make_admin("branch_manager", api=api)
    product, shades, inventory_id = await _polish(api, boss, db, world, make_product)
    s07, s40 = shades["Shade 07"]["variant_id"], shades["Shade 40"]["variant_id"]
    await _count(api, manager, inventory_id, {s07: 5, s40: 3})

    customer = await make_customer()
    await _branch_context(app_redis, world, customer)
    assert (await _add(api, customer, product, s40, 2)).status_code == 200
    # More than the shade has: refused, though the other shade has plenty.
    r = await _add(api, customer, product, s40, 2)
    assert r.status_code == 400 and "Only 3" in r.text, r.text
    r = await _checkout(api, customer)
    assert r.status_code == 201, r.text
    order_id = r.json()["data"]["order_id"]

    item = (await db.execute(select(OrderItem).where(OrderItem.order_id == uuid.UUID(order_id)))).scalar_one()
    assert (str(item.variant_id), item.variant_name, item.quantity) == (s40, "Shade 40", 2)
    assert item.unit_price == Decimal("400.00")
    assert await stock_keeping.available(db, world.branch_id, product, s40) == 1
    assert await stock_keeping.available(db, world.branch_id, product, s07) == 5

    r = await api.get(f"/api/v1/orders/{order_id}", headers=customer.headers)
    assert r.status_code == 200, r.text
    assert r.json()["data"]["items"][0]["variant_name"] == "Shade 40"

    await stock_keeping.give_back(db, world.branch_id, product, uuid.UUID(s40), 2)
    await db.commit()
    assert await stock_keeping.available(db, world.branch_id, product, s40) == 3


async def test_a_line_without_a_shade_is_refused(
    api, db, app_redis, world, make_admin, make_product, make_customer, fake_courier
):
    from app.services.cart_service import CartService

    boss = await make_admin("super_admin", api=api)
    manager = await make_admin("branch_manager", api=api)
    product, shades, inventory_id = await _polish(api, boss, db, world, make_product)
    await _count(api, manager, inventory_id, {shades["Shade 07"]["variant_id"]: 5})
    other = await _polish(api, boss, db, world, make_product)

    customer = await make_customer()
    await _branch_context(app_redis, world, customer)
    # The old app (1.2.9) sends no variant: told to update, in the shopper's language.
    r = await _add(api, customer, product)
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["code"] == "OPTION_REQUIRED"
    assert "update" in r.json()["detail"]["message"].lower()
    r = await api.post("/api/v1/cart/items", headers={**customer.headers, "Accept-Language": "si"},
                       json={"product_id": str(product), "quantity": 1, "price": "1", "name": "x"})
    assert r.status_code == 409 and "update" in r.json()["detail"]["message"]
    assert "තෝරන්න" in r.json()["detail"]["message"]
    # Another product's shade, or one that does not exist, is no shade of this one.
    for wrong in (other[1]["Shade 07"]["variant_id"], str(uuid.uuid4())):
        assert (await _add(api, customer, product, wrong)).status_code == 409

    # A guest cart merged at sign-in skips the add check: checkout is the gate.
    await CartService.add_item(user_id=str(customer.user_id), product_id=str(product), quantity=1,
                               price=1.0, name="ZZ polish")
    r = await _checkout(api, customer)
    assert r.status_code in (400, 409, 422), r.text
    assert "update" in r.text.lower()


async def test_a_retired_shade_cannot_be_bought_and_keeps_its_id(
    api, db, app_redis, world, make_admin, make_product, make_customer
):
    boss = await make_admin("super_admin", api=api)
    manager = await make_admin("branch_manager", api=api)
    product, shades, inventory_id = await _polish(api, boss, db, world, make_product)
    s07, s40 = shades["Shade 07"]["variant_id"], shades["Shade 40"]["variant_id"]
    await _count(api, manager, inventory_id, {s07: 4, s40: 6})

    # Shade 40 is left out, Shade 07 renamed: ids kept, 40 retired, not deleted.
    after = await _shades(api, boss, product, [
        {"variant_id": s07, "name": "Shade 07 Red"},
        {"variant_id": shades["Shade 68"]["variant_id"], "name": "Shade 68"},
    ])
    r = await api.get(f"/api/v1/admin/products/{product}/options", headers=boss.headers)
    every = {o["variant_id"]: o for o in r.json()["data"]["options"]}
    assert every[s40]["is_active"] is False and every[s07]["name"] == "Shade 07 Red"
    assert after["Shade 07 Red"]["variant_id"] == s07
    # Its packs no longer count as "in stock".
    data = await _count(api, manager, inventory_id, {s07: 4})
    assert data["stock_quantity"] == 4

    customer = await make_customer()
    await _branch_context(app_redis, world, customer)
    assert (await _add(api, customer, product, s40)).status_code == 409
    r = await api.get(f"/api/v1/products/{product}", headers=customer.headers)
    assert [o["name"] for o in r.json()["data"]["options"]] == ["Shade 07 Red", "Shade 68"]


async def test_admin_rules(api, db, world, make_admin, make_product):
    boss = await make_admin("super_admin", api=api)
    manager = await make_admin("branch_manager", api=api)
    product = await make_product()
    url = f"/api/v1/admin/products/{product}/options"

    r = await api.put(url, json={"option_label": "Shade", "options": [{"name": "Red"}]},
                      headers=manager.headers)
    assert r.status_code == 403, r.text
    for bad in ([{"name": "Red"}, {"name": "red"}], [{"name": "Red", "price": "0"}], []):
        r = await api.put(url, json={"option_label": "Shade", "options": bad}, headers=boss.headers)
        assert r.status_code in (400, 422), (bad, r.text)

    # Back to a plain product: every shade retired, counted in units again.
    await _shades(api, boss, product, [{"name": "Red"}, {"name": "Blue"}], label="Colour")
    r = await api.put(url, json={"option_label": None, "options": []}, headers=boss.headers)
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["option_label"] is None and data["stock_mode"] == "unit"
    assert all(not o["is_active"] for o in data["options"])


async def test_two_checkouts_for_the_last_of_a_shade(
    api, db, app_redis, world, make_admin, make_product, make_customer, fake_courier
):
    boss = await make_admin("super_admin", api=api)
    manager = await make_admin("branch_manager", api=api)
    product, shades, inventory_id = await _polish(api, boss, db, world, make_product)
    s07 = shades["Shade 07"]["variant_id"]
    await _count(api, manager, inventory_id, {s07: 1})

    a, b = await make_customer(), await make_customer()
    for c in (a, b):
        await _branch_context(app_redis, world, c)
        assert (await _add(api, c, product, s07)).status_code == 200
    results = await asyncio.gather(_checkout(api, a), _checkout(api, b))
    codes = sorted(r.status_code for r in results)
    assert codes[0] == 201 and codes[1] != 201, [r.text for r in results]
    assert await stock_keeping.available(db, world.branch_id, product, s07) == 0


async def test_another_products_shade_never_touches_its_stock(
    api, db, app_redis, world, make_admin, make_product, make_customer, fake_courier
):
    """Review 2026-11-25: a client sending product A with product B's shade id."""
    from app.models.order import OrderItem

    boss = await make_admin("super_admin", api=api)
    manager = await make_admin("branch_manager", api=api)
    polish, shades, inventory_id = await _polish(api, boss, db, world, make_product)
    s07 = shades["Shade 07"]["variant_id"]
    await _count(api, manager, inventory_id, {s07: 5})
    soap = await make_product(price="120.00", stock=10)

    customer = await make_customer()
    await _branch_context(app_redis, world, customer)
    for vid in (s07, str(uuid.uuid4())):
        r = await _add(api, customer, soap, vid)
        assert r.status_code == 200, r.text
        r = await _checkout(api, customer)
        assert r.status_code == 201, r.text
        item = (await db.execute(select(OrderItem).where(
            OrderItem.order_id == uuid.UUID(r.json()["data"]["order_id"])))).scalar_one()
        assert item.variant_id is None and item.unit_price == Decimal("120.00")
    assert await stock_keeping.available(db, world.branch_id, polish, s07) == 5, "the shade is untouched"
    assert await stock_keeping.available(db, world.branch_id, soap) == 8


async def test_a_product_edit_cannot_switch_a_shade_products_stock(api, db, world, make_admin, make_product):
    boss = await make_admin("super_admin", api=api)
    product, _, _ = await _polish(api, boss, db, world, make_product)
    for body in ({"stock_mode": "unit"}, {"sold_by_weight": True}):
        r = await api.put(f"/api/v1/admin/products/{product}", json=body, headers=boss.headers)
        assert r.status_code in (400, 409), (body, r.text)
    # A price change is fine (the admin form always sends the category).
    from tests.integration.test_admin_catalog import _category, _id

    cat = _id(await _category(api, boss), "category_id", "id")
    r = await api.put(f"/api/v1/admin/products/{product}", json={"price": "420.00", "category_id": cat},
                      headers=boss.headers)
    assert r.status_code == 200, r.text
    assert r.json()["data"]["stock_mode"] == "packed"
