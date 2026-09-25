"""
The storefront a guest and a customer browse: choosing where you are, then the
catalogue that branch sells -- lists, search, categories, product pages,
reviews, banners and the location pickers.

The rule under test throughout: a shopper sees only what their branch stocks
and has switched on, in their language, at that branch's price.
"""
from __future__ import annotations

import uuid

import pytest


def _device() -> dict:
    return {"X-Device-Id": f"zz-device-{uuid.uuid4().hex[:10]}"}


async def _choose_location(api, world, headers) -> dict:
    r = await api.post(
        "/api/v1/branch/resolve-by-location",
        json={"province": world.province, "district": world.district, "postalCity": world.postal_city},
        headers=headers,
    )
    assert r.status_code == 200, r.text
    return r.json()


async def test_a_guest_chooses_a_location_and_browses(api, world, make_product):
    visible = await make_product(price="120.00")
    headers = _device()

    # No location yet: the catalogue does not guess a branch.
    r = await api.get("/api/v1/products", headers=headers)
    assert r.status_code in (400, 404, 422), r.text

    ctx = await _choose_location(api, world, headers)
    assert str(world.branch_id) in str(ctx)

    r = await api.get("/api/v1/products", headers=headers)
    assert r.status_code == 200, r.text
    assert str(visible) in r.text

    for path in [
        "/api/v1/products/featured",
        "/api/v1/products/home-feed",
        f"/api/v1/products/{visible}",
        f"/api/v1/products/{visible}/variants",
        f"/api/v1/products/{visible}/reviews",
    ]:
        r = await api.get(path, headers=headers)
        assert r.status_code == 200, (path, r.text)



async def test_a_customers_branch_context_follows_their_account(api, world, make_customer):
    c = await make_customer()
    await _choose_location(api, world, c.headers)
    r = await api.get("/api/v1/branch/context", headers=c.headers)
    assert r.status_code == 200 and str(world.branch_id) in r.text
    r = await api.post("/api/v1/branch/clear", headers=c.headers)
    assert r.status_code == 200, r.text
    r = await api.get("/api/v1/branch/context", headers=c.headers)
    assert str(world.branch_id) not in r.text


async def test_an_unserved_location_is_refused_plainly(api, world):
    r = await api.post(
        "/api/v1/branch/resolve-by-location",
        json={"province": world.province, "district": world.district, "postalCity": "Nowhere Town"},
        headers=_device(),
    )
    assert r.status_code == 404, r.text
    assert "LOCATION_NOT_SERVED" in r.text


async def test_the_catalogue_shows_only_what_the_branch_sells(api, db, world, make_product):
    from sqlalchemy import update

    from app.models.product import BranchInventory

    on_sale = await make_product(price="100.00")
    hidden = await make_product(price="100.00")
    sold_out = await make_product(price="100.00", stock=0)
    inactive = await make_product(price="100.00", active=False)
    await db.execute(
        update(BranchInventory).where(BranchInventory.product_id == hidden).values(is_active=False)
    )
    await db.commit()

    headers = _device()
    await _choose_location(api, world, headers)
    r = await api.get("/api/v1/products", params={"limit": 100}, headers=headers)
    assert str(on_sale) in r.text
    for p in (hidden, sold_out, inactive):
        assert str(p) not in r.text, f"{p} must not be listed"
    r = await api.get(f"/api/v1/products/{hidden}", headers=headers)
    assert r.status_code == 404


async def test_branch_price_and_discount_on_the_product_page(api, world, make_product):
    p = await make_product(price="500.00", branch_price="400.00", discount=10.0)
    headers = _device()
    await _choose_location(api, world, headers)
    r = await api.get(f"/api/v1/products/{p}", headers=headers)
    data = r.json()["data"]
    assert float(data["price"]) == pytest.approx(400.0)
    assert float(data["discount_price"]) == pytest.approx(360.0)


async def test_search_and_sorting(api, world, make_product):
    cheap = await make_product(price="50.00")
    dear = await make_product(price="5000.00")
    headers = _device()
    await _choose_location(api, world, headers)

    r = await api.get("/api/v1/products/search", params={"q": "ZZ Test Rice"}, headers=headers)
    assert r.status_code == 200, r.text

    r = await api.get(
        "/api/v1/products",
        params={"min_price": 40, "max_price": 60, "sort_by": "price", "sort_order": "asc", "limit": 100},
        headers=headers,
    )
    assert r.status_code == 200, r.text
    assert str(cheap) in r.text and str(dear) not in r.text

    r = await api.get("/api/v1/products", params={"sort_by": "DROP TABLE"}, headers=headers)
    assert r.status_code == 422, "sort columns are an allow-list"


async def test_products_are_localised(api, world, make_product, db):
    from sqlalchemy import update

    from app.models.product import Product

    p = await make_product()
    await db.execute(update(Product).where(Product.product_id == p).values(name_si="සහල්", name_ta="அரிசி"))
    await db.commit()
    headers = _device()
    await _choose_location(api, world, headers)
    for lang, word in [("si", "සහල්"), ("ta", "அரிசி")]:
        r = await api.get(f"/api/v1/products/{p}", headers={**headers, "Accept-Language": lang})
        assert r.status_code == 200
        assert word in r.text, lang


async def test_categories_banners_and_location_pickers(api, world, make_product):
    headers = _device()
    await _choose_location(api, world, headers)
    for path in ["/api/v1/categories", "/api/v1/banners", "/api/v1/locations/provinces"]:
        r = await api.get(path, headers=headers)
        assert r.status_code == 200, (path, r.text)

    r = await api.get("/api/v1/locations/provinces", params={"include_upcoming": True})
    assert world.province in r.text
    r = await api.get("/api/v1/locations/districts", params={"province": world.province})
    assert r.status_code == 200 and world.district in r.text
    r = await api.get("/api/v1/locations/postal-cities", params={"district": world.district})
    assert r.status_code == 200 and world.postal_city in r.text

    r = await api.get(f"/api/v1/categories/{uuid.uuid4()}", headers=headers)
    assert r.status_code == 404

    r = await api.post("/api/v1/banners/events", json={"banner_id": str(uuid.uuid4()), "event": "view"},
                       headers=headers)
    assert r.status_code in (200, 202, 204, 404, 422), r.text


async def test_app_bootstrap_endpoints(api):
    for path in ["/api/v1/app/app-config", "/api/v1/app/splash-config", "/api/v1/app/support-info", "/api"]:
        r = await api.get(path)
        assert r.status_code == 200, (path, r.text)
