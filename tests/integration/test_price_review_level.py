"""
Price review level: how often a product's shelf price is re-checked.

The super admin picks daily / weekly / monthly / long when adding a product
(long when they choose nothing -- no product is ever without one), and the
Sribees Price app lists each level on its own tab through ?price_review_level=.
"""
from __future__ import annotations

from tests.integration.test_admin_catalog import _category, _id, _product, _product_body


async def _list(api, admin, **params):
    r = await api.get("/api/v1/admin/products", params={"limit": 100, **params}, headers=admin.headers)
    assert r.status_code == 200, r.text
    return {p["product_id"] for p in r.json()["data"]["products"]}


async def test_the_level_chosen_on_create_is_stored_and_filters_the_list(api, world, make_admin):
    admin = await make_admin("super_admin", api=api)
    cat = _id(await _category(api, admin), "category_id", "id")

    daily = await _product(api, admin, cat, price_review_level="daily")
    default = await _product(api, admin, cat)
    assert daily["price_review_level"] == "daily"
    assert default["price_review_level"] == "long"

    daily_ids = await _list(api, admin, category_id=cat, price_review_level="daily")
    long_ids = await _list(api, admin, category_id=cat, price_review_level="long")
    assert daily_ids == {daily["product_id"]}
    assert long_ids == {default["product_id"]}


async def test_the_super_admin_moves_a_product_between_levels(api, world, make_admin):
    admin = await make_admin("super_admin", api=api)
    cat = _id(await _category(api, admin), "category_id", "id")
    pid = (await _product(api, admin, cat, price_review_level="long"))["product_id"]

    r = await api.put(f"/api/v1/admin/products/{pid}", json={"price_review_level": "weekly"},
                      headers=admin.headers)
    assert r.status_code == 200, r.text
    assert r.json()["data"]["price_review_level"] == "weekly"

    # A save that does not send the field, or sends null, leaves it alone.
    r = await api.put(f"/api/v1/admin/products/{pid}", json={"price": "460.00"}, headers=admin.headers)
    assert r.status_code == 200, r.text
    assert r.json()["data"]["price_review_level"] == "weekly"
    r = await api.put(f"/api/v1/admin/products/{pid}", json={"price_review_level": None},
                      headers=admin.headers)
    assert r.status_code == 200, r.text
    assert r.json()["data"]["price_review_level"] == "weekly"


async def test_an_unknown_level_is_refused(api, world, make_admin):
    admin = await make_admin("super_admin", api=api)
    cat = _id(await _category(api, admin), "category_id", "id")
    r = await api.post("/api/v1/admin/products",
                       json=_product_body(cat, price_review_level="yearly"), headers=admin.headers)
    assert r.status_code == 422, r.text
    r = await api.get("/api/v1/admin/products", params={"price_review_level": "unset"},
                      headers=admin.headers)
    assert r.status_code == 422, r.text


async def test_a_branch_manager_cannot_change_the_level(api, db, world, make_admin, make_product):
    from sqlalchemy import select

    from app.models.product import Product

    product = await make_product()
    manager = await make_admin("branch_manager", api=api)
    r = await api.put(f"/api/v1/admin/products/{product}", json={"price_review_level": "daily"},
                      headers=manager.headers)
    assert r.status_code == 200, r.text

    db.expire_all()
    level = (await db.execute(
        select(Product.price_review_level).where(Product.product_id == product)
    )).scalar_one()
    assert level == "long", "the level is the super admin's to set"
