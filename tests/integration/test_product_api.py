"""
The Super Admin product API under /products (the admin panel uses
/admin/products; these write routes stay for scripts and the Super Admin),
and the storefront reads beside it: by category, by id or slug, variants.

Each write route is pinned for its happy path and for every refusal it
makes, so a product can never be created without what the storefront needs
to show it.
"""
from __future__ import annotations

import uuid

URL = "/api/v1/products"


def _tag() -> str:
    return uuid.uuid4().hex[:8]


async def _category(api, boss, parent=None) -> str:
    t = _tag()
    body = {"name": f"ZZ Cat {t}", "slug": f"zz-cat-{t}"}
    if parent:
        body["parent_category_id"] = parent
    else:
        body["image_url"] = "/uploads/categories/zz.png"
    r = await api.post("/api/v1/admin/categories", json=body, headers=boss.headers)
    assert r.status_code == 201, r.text
    return r.json()["data"]["category_id"]


def _product(category_id: str, **over) -> dict:
    t = _tag()
    return {
        "name": f"ZZ API Rice {t}", "slug": f"zz-api-rice-{t}", "sku": f"ZZ-API-{t}", "price": "320.00",
        "category_id": category_id, "weight": "1.000", "weight_unit": "kg",
        "images": [{"image_url": "/uploads/products/zz.png", "is_primary": True}], **over,
    }


async def test_creating_a_product_needs_what_the_storefront_shows(api, world, make_admin):
    boss = await make_admin("super_admin", api=api)
    cat = await _category(api, boss)
    other = await _category(api, boss)
    stray_sub = await _category(api, boss, parent=other)

    refusals = [
        ({"images": []}, "a thumbnail"),
        ({"weight": "0"}, "a weight"),
        ({"weight_unit": " "}, "a weight unit"),
        ({"subcategory_id": stray_sub}, "a sub-category of this category"),
    ]
    for over, why in refusals:
        r = await api.post(URL, json=_product(cat, **over), headers=boss.headers)
        assert r.status_code == 400, (why, r.text)

    body = _product(cat)
    r = await api.post(URL, json=body, headers=boss.headers)
    assert r.status_code == 201, r.text
    r = await api.post(URL, json={**_product(cat), "slug": body["slug"]}, headers=boss.headers)
    assert r.status_code == 400, "slugs are unique"


async def test_editing_and_removing_a_product(api, db, app_redis, world, make_admin, make_customer):
    boss = await make_admin("super_admin", api=api)
    cat = await _category(api, boss)
    first = _product(cat)
    second = _product(cat)
    pid = (await api.post(URL, json=first, headers=boss.headers)).json()["data"]["product_id"]
    await api.post(URL, json=second, headers=boss.headers)

    r = await api.put(f"{URL}/{pid}", json={"name": "ZZ Renamed", "price": "300.00"}, headers=boss.headers)
    assert r.status_code == 200 and "ZZ Renamed" in r.text, r.text
    r = await api.put(f"{URL}/{first['slug']}", json={"short_description": "by slug"}, headers=boss.headers)
    assert r.status_code == 200, "a product can be addressed by its slug"
    for body, code, why in [
        ({"weight": "-1"}, 400, "a weight must be positive"),
        ({"slug": second["slug"]}, 400, "the slug is another product's"),
        ({"subcategory_id": str(uuid.uuid4())}, 400, "an unknown sub-category"),
    ]:
        r = await api.put(f"{URL}/{pid}", json=body, headers=boss.headers)
        assert r.status_code == code, (why, r.text)
    r = await api.put(f"{URL}/{uuid.uuid4()}", json={"name": "x"}, headers=boss.headers)
    assert r.status_code == 404

    # Images and variants.
    r = await api.post(f"{URL}/{pid}/images", json={"image_url": "/uploads/products/b.png", "sort_order": 1},
                       headers=boss.headers)
    assert r.status_code == 201, r.text
    image_id = r.json()["data"]["image_id"]
    r = await api.delete(f"{URL}/{pid}/images/{image_id}", headers=boss.headers)
    assert r.status_code == 200, r.text
    r = await api.delete(f"{URL}/{pid}/images/not-a-uuid", headers=boss.headers)
    assert r.status_code == 400
    r = await api.post(f"{URL}/{uuid.uuid4()}/images", json={"image_url": "/x.png"}, headers=boss.headers)
    assert r.status_code == 404

    r = await api.post(f"{URL}/{pid}/variants", json={"name": "5 kg", "price": "1500", "stock_quantity": 4},
                       headers=boss.headers)
    assert r.status_code == 201, r.text
    variant_id = r.json()["data"]["variant_id"]
    r = await api.post(f"{URL}/{uuid.uuid4()}/variants", json={"name": "x", "price": "1"}, headers=boss.headers)
    assert r.status_code == 404

    # Stock, for the product and for one variant.
    r = await api.patch(f"{URL}/{pid}/stock", params={"quantity": 7}, headers=boss.headers)
    assert r.status_code == 200, r.text
    r = await api.patch(f"{URL}/{pid}/stock", params={"quantity": 2, "variant_id": variant_id}, headers=boss.headers)
    assert r.status_code == 200, r.text
    r = await api.patch(f"{URL}/{pid}/stock", params={"quantity": 2, "variant_id": "nope"}, headers=boss.headers)
    assert r.status_code == 400
    r = await api.patch(f"{URL}/{uuid.uuid4()}/stock", params={"quantity": 1}, headers=boss.headers)
    assert r.status_code == 404

    # The storefront reads, for a customer shopping this branch.
    import json

    from app.config.redis import RedisKeys
    from app.models.product import BranchInventory
    from app.services.branch_service import _build_branch_context

    db.add(BranchInventory(product_id=uuid.UUID(pid), branch_id=world.branch_id, stock_quantity=5, is_active=True))
    await db.commit()
    customer = await make_customer()
    await app_redis.setex(RedisKeys.branch_context(str(customer.user_id)), 600,
                          json.dumps(_build_branch_context(world.branch_id, "ZZ", world.postal_city)))
    for path in [f"{URL}/{pid}", f"{URL}/{first['slug']}", f"{URL}/{pid}/variants", f"{URL}/category/{cat}",
                 f"{URL}/category/{cat}?page=2&limit=1"]:
        r = await api.get(path, headers=customer.headers)
        assert r.status_code == 200, (path, r.text)
    r = await api.get(f"{URL}/{uuid.uuid4()}", headers=customer.headers)
    assert r.status_code == 404
    r = await api.get(f"{URL}/{uuid.uuid4()}/variants", headers=customer.headers)
    assert r.status_code == 404

    # Removing it takes it off the storefront.
    r = await api.delete(f"{URL}/{pid}", headers=boss.headers)
    assert r.status_code == 200, r.text
    r = await api.delete(f"{URL}/{uuid.uuid4()}", headers=boss.headers)
    assert r.status_code == 404


async def test_only_the_super_admin_writes_products(api, world, make_admin):
    boss = await make_admin("super_admin", api=api)
    cat = await _category(api, boss)
    manager = await make_admin("inventory_manager", api=api)
    r = await api.post(URL, json=_product(cat), headers=manager.headers)
    assert r.status_code == 403
