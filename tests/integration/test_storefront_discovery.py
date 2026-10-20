"""
How a shopper finds things on the web storefront: category tiles with honest
counts, the filter panel (categories, stars, promotions), Popular Picks and
the hero banner's copy.

Each test builds its own branch (the `world` fixture), so products, sales and
reviews from other tests never leak into what these ones see.
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import update

from tests.integration.test_storefront import _choose_location, _device


async def _category(db, world, *, parent=None) -> uuid.UUID:
    from app.models.category import BranchCategory, Category

    cid = uuid.uuid4()
    tag = cid.hex[:6]
    db.add(Category(
        category_id=cid, name=f"ZZ Cat {tag}", slug=f"zz-cat-{tag}",
        parent_category_id=parent, is_active=True,
    ))
    await db.flush()
    db.add(BranchCategory(category_id=cid, branch_id=world.branch_id, is_active=True))
    await db.commit()
    return cid


async def _file(db, product_id, category_id, subcategory_id=None, **fields) -> None:
    from app.models.product import Product

    await db.execute(
        update(Product)
        .where(Product.product_id == product_id)
        .values(category_id=category_id, subcategory_id=subcategory_id, **fields)
    )
    await db.commit()


async def _review(db, product_id, user_id, rating: int) -> None:
    from app.models.product import Review

    db.add(Review(product_id=product_id, user_id=user_id, rating=rating, is_approved=True))
    await db.commit()


def _ids(r) -> list:
    assert r.status_code == 200, r.text
    return [p["product_id"] for p in r.json()["data"]["products"]]


async def test_a_category_counts_what_the_shopper_can_buy(api, db, world, make_product):
    from app.models.product import BranchInventory

    parent = await _category(db, world)
    child = await _category(db, world, parent=parent)
    in_parent = await make_product()
    in_child = await make_product()
    sold_out = await make_product(stock=0)
    switched_off = await make_product()
    await _file(db, in_parent, parent)
    await _file(db, in_child, parent, child)
    await _file(db, sold_out, parent)
    await _file(db, switched_off, parent, child)
    await db.execute(
        update(BranchInventory)
        .where(BranchInventory.product_id == switched_off)
        .values(is_active=False)
    )
    await db.commit()

    headers = _device()
    await _choose_location(api, world, headers)
    r = await api.get("/api/v1/categories", headers=headers)
    counts = {c["category_id"]: c["product_count"] for c in r.json()["data"]["categories"]}
    # The parent tile counts its sub-category's product too, as tapping it shows.
    assert counts[str(parent)] == 2
    assert counts[str(child)] == 1

    r = await api.get("/api/v1/products", params={"category_id": str(parent)}, headers=headers)
    assert len(_ids(r)) == counts[str(parent)]

    r = await api.get("/api/v1/categories", params={"hierarchical": True}, headers=headers)
    tree = {c["category_id"]: c for c in r.json()["data"]["categories"]}
    assert tree[str(parent)]["product_count"] == 2
    assert tree[str(parent)]["children"][0]["product_count"] == 1


async def test_a_product_on_a_second_shelf_lists_and_counts_there(api, db, world, make_product):
    """One home in the product row, a second shelf through product_extra_categories."""
    from app.models.product import ProductExtraCategory

    dairy = await _category(db, world)
    milk_powder = await _category(db, world, parent=dairy)
    beverages = await _category(db, world)
    tea = await _category(db, world, parent=beverages)
    sachet = await make_product()
    tea_only = await make_product()
    await _file(db, sachet, dairy, milk_powder)
    await _file(db, tea_only, beverages, tea)
    db.add(ProductExtraCategory(product_id=sachet, subcategory_id=tea))
    await db.commit()

    headers = _device()
    await _choose_location(api, world, headers)

    # Both shelves list it, by sub-category and by parent alike.
    for shelf, expected in (
        (milk_powder, {sachet}), (dairy, {sachet}),
        (tea, {sachet, tea_only}), (beverages, {sachet, tea_only}),
    ):
        r = await api.get("/api/v1/products", params={"category_id": str(shelf)}, headers=headers)
        assert set(_ids(r)) == {str(x) for x in expected}, shelf
    r = await api.get(
        "/api/v1/products",
        params={"category_id": str(beverages), "subcategory_id": str(tea)},
        headers=headers,
    )
    assert set(_ids(r)) == {str(sachet), str(tea_only)}

    # Tiles agree with what tapping them shows; the home is unchanged.
    r = await api.get("/api/v1/categories", headers=headers)
    counts = {c["category_id"]: c["product_count"] for c in r.json()["data"]["categories"]}
    assert counts[str(milk_powder)] == 1 and counts[str(dairy)] == 1
    assert counts[str(tea)] == 2 and counts[str(beverages)] == 2
    r = await api.get(f"/api/v1/products/{sachet}", headers=headers)
    assert r.json()["data"]["subcategory_id"] == str(milk_powder)


async def test_the_filter_panel(api, db, world, make_product, make_customer):
    from app.models.product import BranchInventory

    veg = await _category(db, world)
    dairy = await _category(db, world)
    rice = await _category(db, world)
    loved = await make_product()
    liked = await make_product()
    on_sale = await make_product()
    cashback = await make_product()
    for p, c in ((loved, veg), (liked, dairy), (on_sale, rice), (cashback, rice)):
        await _file(db, p, c)
    await db.execute(
        update(BranchInventory).where(BranchInventory.product_id == on_sale).values(is_on_sale=True)
    )
    await db.execute(
        update(BranchInventory).where(BranchInventory.product_id == cashback)
        .values(cashback_percentage=25.0)
    )
    await db.commit()

    a, b = await make_customer(), await make_customer()
    await _review(db, loved, a.user_id, 5)
    await _review(db, loved, b.user_id, 4)
    await _review(db, liked, a.user_id, 3)

    headers = _device()
    await _choose_location(api, world, headers)

    # Several categories at once.
    r = await api.get("/api/v1/products", params={"category_id": f"{veg},{dairy}"}, headers=headers)
    assert set(_ids(r)) == {str(loved), str(liked)}

    # "4 stars & up" -- and the card carries its real stars.
    r = await api.get("/api/v1/products", params={"min_rating": 4, "limit": 100}, headers=headers)
    cards = {p["product_id"]: p for p in r.json()["data"]["products"]}
    assert str(loved) in cards and str(liked) not in cards
    assert cards[str(loved)]["average_rating"] == pytest.approx(4.5)
    assert cards[str(loved)]["review_count"] == 2

    # Promotions: each kind alone, then either.
    r = await api.get("/api/v1/products", params={"promotion": "sale", "category_id": str(rice)}, headers=headers)
    assert _ids(r) == [str(on_sale)]
    r = await api.get("/api/v1/products", params={"promotion": "cashback", "category_id": str(rice)}, headers=headers)
    assert _ids(r) == [str(cashback)]
    r = await api.get(
        "/api/v1/products",
        params=[("promotion", "sale"), ("promotion", "cashback"), ("limit", "100")],
        headers=headers,
    )
    assert {str(on_sale), str(cashback)} <= set(_ids(r))
    assert str(loved) not in _ids(r)
    r = await api.get("/api/v1/products", params={"promotion": "bogus"}, headers=headers)
    assert r.status_code == 422

    # Best rated first; unrated products after every rated one.
    r = await api.get(
        "/api/v1/products",
        params={"sort_by": "rating", "category_id": f"{veg},{dairy},{rice}"},
        headers=headers,
    )
    ids = _ids(r)
    assert ids[:2] == [str(loved), str(liked)]


async def test_popular_picks_rank_by_recent_sales_then_fill(
    api, db, world, make_product, make_customer, place_order
):
    from app.models.order import Order

    best = await make_product(price="100.00")
    second = await make_product(price="100.00")
    cancelled = await make_product(price="100.00")
    featured = await make_product(price="100.00")
    await _file(db, featured, None, is_featured=True)

    c = await make_customer()
    await place_order(c, best, qty=3)
    await place_order(c, second, qty=1)
    order = await place_order(c, cancelled, qty=10)
    await db.execute(
        update(Order).where(Order.order_id == uuid.UUID(order["order_id"])).values(status="cancelled")
    )
    await db.commit()

    headers = _device()
    await _choose_location(api, world, headers)
    r = await api.get("/api/v1/products/popular", headers=headers)
    ids = _ids(r)
    # Sales first; a cancelled order sold nothing; featured tops up the rest.
    assert ids[:3] == [str(best), str(second), str(featured)]
    assert str(cancelled) in ids
    assert len(ids) <= 10
    assert "units_sold" not in r.text

    r = await api.get("/api/v1/products/popular", params={"limit": 11}, headers=headers)
    assert r.status_code == 422


async def test_a_banner_carries_its_description(api, db, world):
    from app.models.banner import Banner

    db.add(Banner(
        title="ZZ Fresh", subtitle="Straight to you", description="Picked this morning.",
        branch_id=world.branch_id, is_active=True,
    ))
    await db.commit()

    headers = _device()
    await _choose_location(api, world, headers)
    r = await api.get("/api/v1/banners", headers=headers)
    banner = next(b for b in r.json()["data"]["banners"] if b["title"] == "ZZ Fresh")
    assert banner["description"] == "Picked this morning."
