"""
Admin catalogue: categories, products, images and the helpers around them.

Every route the admin panel's Products and Categories screens call, over HTTP,
as the roles that use them. Each test builds its own names, so they can run in
any order against the same database.
"""
from __future__ import annotations

import base64
import uuid

import pytest

# The smallest valid PNG: 1x1, transparent. Enough for the upload path, which
# checks the declared type and stores the bytes.
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII="
)


def _tag() -> str:
    return uuid.uuid4().hex[:8]


async def _category(api, admin, *, parent=None, name=None) -> dict:
    t = _tag()
    body = {
        "name": name or f"ZZ Cat {t}",
        "slug": f"zz-cat-{t}",
        "name_si": "කාණ්ඩය",
        "name_ta": "வகை",
        "image_url": "/uploads/categories/zz.png",
    }
    if parent:
        # A sub-category inherits its parent's image and may not have its own.
        body["parent_category_id"] = parent
        del body["image_url"]
    r = await api.post("/api/v1/admin/categories", json=body, headers=admin.headers)
    assert r.status_code == 201, r.text
    return r.json()["data"]


def _product_body(category_id: str, **over) -> dict:
    t = _tag()
    body = {
        "name": f"ZZ Basmati {t}",
        "name_si": "බාස්මතී",
        "name_ta": "பாஸ்மதி",
        "slug": f"zz-basmati-{t}",
        "sku": f"ZZ-BAS-{t}".upper(),
        "price": "450.00",
        "category_id": category_id,
        "weight": "1.000",
        "weight_unit": "kg",
        "images": [{"image_url": "/uploads/products/zz.png", "is_primary": True}],
    }
    body.update(over)
    return body


async def _product(api, admin, category_id, **over) -> dict:
    r = await api.post("/api/v1/admin/products", json=_product_body(category_id, **over), headers=admin.headers)
    assert r.status_code == 201, r.text
    return r.json()["data"]


def _id(obj: dict, *keys: str) -> str:
    for k in keys:
        if obj.get(k):
            return str(obj[k])
    raise AssertionError(f"none of {keys} in {list(obj)}")


# ---------------------------------------------------------------------------
# Categories
# ---------------------------------------------------------------------------


async def test_category_lifecycle(api, world, make_admin):
    admin = await make_admin("super_admin", api=api)
    parent = await _category(api, admin)
    parent_id = _id(parent, "category_id", "id")

    child = await _category(api, admin, parent=parent_id)
    child_id = _id(child, "category_id", "id")

    r = await api.get("/api/v1/admin/categories", headers=admin.headers)
    assert r.status_code == 200, r.text
    assert parent_id in r.text and child_id in r.text

    r = await api.put(
        f"/api/v1/admin/categories/{parent_id}",
        json={"name": "ZZ Renamed", "name_si": "නව නම"},
        headers=admin.headers,
    )
    assert r.status_code == 200, r.text
    assert "ZZ Renamed" in r.text

    # A parent with children cannot go; the child can, then the parent.
    r = await api.delete(f"/api/v1/admin/categories/{parent_id}", headers=admin.headers)
    assert r.status_code == 400, r.text
    r = await api.delete(f"/api/v1/admin/categories/{child_id}", headers=admin.headers)
    assert r.status_code == 200, r.text
    r = await api.delete(f"/api/v1/admin/categories/{parent_id}", headers=admin.headers)
    assert r.status_code == 200, r.text
    r = await api.delete(f"/api/v1/admin/categories/{parent_id}", headers=admin.headers)
    assert r.status_code == 404


async def test_a_category_with_products_cannot_be_deleted(api, world, make_admin):
    admin = await make_admin("super_admin", api=api)
    cat = _id(await _category(api, admin), "category_id", "id")
    await _product(api, admin, cat)
    r = await api.delete(f"/api/v1/admin/categories/{cat}", headers=admin.headers)
    assert r.status_code == 400
    assert "products" in r.text.lower()


async def test_category_slugs_are_unique(api, world, make_admin):
    admin = await make_admin("super_admin", api=api)
    c = await _category(api, admin)
    r = await api.post(
        "/api/v1/admin/categories",
        json={"name": "Dup", "slug": c["slug"], "image_url": "/uploads/categories/zz.png"},
        headers=admin.headers,
    )
    assert r.status_code == 400, r.text


async def test_category_image_upload_accepts_images_only(api, world, make_admin):
    admin = await make_admin("super_admin", api=api)
    r = await api.post(
        "/api/v1/admin/categories/upload-image",
        files={"file": ("tea.png", PNG, "image/png")},
        headers=admin.headers,
    )
    assert r.status_code == 201, r.text
    assert r.json()["data"]["image_url"]
    r = await api.post(
        "/api/v1/admin/categories/upload-image",
        files={"file": ("evil.html", b"<script>x</script>", "text/html")},
        headers=admin.headers,
    )
    assert r.status_code == 400, r.text


# ---------------------------------------------------------------------------
# Products
# ---------------------------------------------------------------------------


async def test_product_lifecycle(api, world, make_admin):
    admin = await make_admin("super_admin", api=api)
    cat = _id(await _category(api, admin), "category_id", "id")
    created = await _product(api, admin, cat)
    pid = _id(created, "product_id", "id")

    r = await api.get(f"/api/v1/admin/products/{pid}", headers=admin.headers)
    assert r.status_code == 200, r.text
    assert r.json()["data"]["name"] == created["name"]

    r = await api.get(
        "/api/v1/admin/products",
        params={"search": created["name"], "category_id": cat, "sort_by": "price", "sort_order": "asc"},
        headers=admin.headers,
    )
    assert r.status_code == 200, r.text
    assert pid in r.text

    r = await api.put(
        f"/api/v1/admin/products/{pid}",
        json={"price": "499.00", "is_featured": True, "description": "Long grain"},
        headers=admin.headers,
    )
    assert r.status_code == 200, r.text
    r = await api.get(f"/api/v1/admin/products/{pid}", headers=admin.headers)
    assert float(r.json()["data"]["price"]) == pytest.approx(499.0)

    r = await api.delete(f"/api/v1/admin/products/{pid}", headers=admin.headers)
    assert r.status_code == 200, r.text
    r = await api.delete(f"/api/v1/admin/products/{uuid.uuid4()}", headers=admin.headers)
    assert r.status_code == 404


@pytest.mark.parametrize(
    ("override", "fragment"),
    [
        ({"images": []}, "image"),
        ({"weight": "0"}, "weight"),
        ({"weight": None}, "weight"),
    ],
)
async def test_product_creation_rules(api, world, make_admin, override, fragment):
    admin = await make_admin("super_admin", api=api)
    cat = _id(await _category(api, admin), "category_id", "id")
    r = await api.post("/api/v1/admin/products", json=_product_body(cat, **override), headers=admin.headers)
    assert r.status_code == 400, r.text
    assert fragment in r.text.lower()


async def test_product_slug_and_sku_are_unique(api, world, make_admin):
    admin = await make_admin("super_admin", api=api)
    cat = _id(await _category(api, admin), "category_id", "id")
    p = await _product(api, admin, cat)
    r = await api.post("/api/v1/admin/products", json=_product_body(cat, slug=p["slug"]), headers=admin.headers)
    assert r.status_code == 400 and "slug" in r.text.lower()
    r = await api.post("/api/v1/admin/products", json=_product_body(cat, sku=p["sku"]), headers=admin.headers)
    assert r.status_code == 400 and "sku" in r.text.lower()


async def test_a_subcategory_must_belong_to_its_category(api, world, make_admin):
    admin = await make_admin("super_admin", api=api)
    a = _id(await _category(api, admin), "category_id", "id")
    b = _id(await _category(api, admin), "category_id", "id")
    b_child = _id(await _category(api, admin, parent=b), "category_id", "id")
    r = await api.post(
        "/api/v1/admin/products", json=_product_body(a, subcategory_id=b_child), headers=admin.headers
    )
    assert r.status_code == 400, r.text
    ok = await _product(api, admin, b, subcategory_id=b_child)
    assert ok


async def test_product_images(api, world, make_admin):
    admin = await make_admin("super_admin", api=api)
    cat = _id(await _category(api, admin), "category_id", "id")
    pid = _id(await _product(api, admin, cat), "product_id", "id")

    r = await api.post(
        "/api/v1/admin/products/upload-image",
        files={"file": ("rice.png", PNG, "image/png")},
        headers=admin.headers,
    )
    assert r.status_code == 201, r.text
    path = r.json()["data"]["image_url"]
    assert not path.endswith((".html", ".svg"))

    r = await api.post(
        f"/api/v1/admin/products/{pid}/images",
        json={"image_url": path, "is_primary": False, "alt_text": "back"},
        headers=admin.headers,
    )
    assert r.status_code == 201, r.text
    image_id = _id(r.json()["data"], "image_id", "id")

    r = await api.patch(f"/api/v1/admin/products/{pid}/images/{image_id}/primary", headers=admin.headers)
    assert r.status_code == 200, r.text
    r = await api.delete(f"/api/v1/admin/products/{pid}/images/{image_id}", headers=admin.headers)
    assert r.status_code == 200, r.text

    r = await api.post(
        "/api/v1/admin/products/upload-image",
        files={"file": ("x.svg", b"<svg onload=alert(1)>", "image/svg+xml")},
        headers=admin.headers,
    )
    assert r.status_code == 400, r.text


async def test_sku_suggestions_are_unique_and_readable(api, world, make_admin):
    admin = await make_admin("super_admin", api=api)
    cat = await _category(api, admin, name="Rice & Grains")
    r = await api.post(
        "/api/v1/admin/products/suggest-sku",
        json={"name": "Keeri Samba 5kg", "category_id": _id(cat, "category_id", "id")},
        headers=admin.headers,
    )
    assert r.status_code == 200, r.text
    sku = r.json()["data"]["sku"]
    assert sku.startswith("RICE") and " " not in sku


async def test_keyword_suggestions_fail_politely_without_an_ai_key(api, world, make_admin, monkeypatch):
    from app.config.settings import settings

    monkeypatch.setattr(settings, "gemini_api_key", "", raising=False)
    admin = await make_admin("super_admin", api=api)
    r = await api.post(
        "/api/v1/admin/products/suggest-keywords",
        json={"name": "Coconut oil"},
        headers=admin.headers,
    )
    assert r.status_code == 503, r.text
    assert "detail" in r.json()


async def test_search_misses(api, world, make_admin):
    admin = await make_admin("super_admin", api=api)
    r = await api.get("/api/v1/admin/products/search-misses", headers=admin.headers)
    assert r.status_code == 200, r.text
    r = await api.post(
        "/api/v1/admin/products/search-misses/status",
        json={"query_key": f"zz-none-{_tag()}", "status": "ignored"},
        headers=admin.headers,
    )
    assert r.status_code == 404, r.text
    r = await api.post(
        "/api/v1/admin/products/search-misses/attach",
        json={"query_key": f"zz-none-{_tag()}", "product_id": str(uuid.uuid4())},
        headers=admin.headers,
    )
    assert r.status_code == 404, r.text


# ---------------------------------------------------------------------------
# Who may do what
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("role", ["marketing_manager", "customer_support"])
async def test_read_only_roles_cannot_change_the_catalogue(api, world, make_admin, role):
    boss = await make_admin("super_admin", api=api)
    cat = _id(await _category(api, boss), "category_id", "id")
    pid = _id(await _product(api, boss, cat), "product_id", "id")

    who = await make_admin(role, api=api)
    r = await api.get("/api/v1/admin/products", headers=who.headers)
    assert r.status_code == 200, r.text
    for method, path, body in [
        ("POST", "/api/v1/admin/products", _product_body(cat)),
        ("PUT", f"/api/v1/admin/products/{pid}", {"price": "1"}),
        ("DELETE", f"/api/v1/admin/products/{pid}", None),
        ("POST", "/api/v1/admin/categories", {"name": "x", "slug": f"x-{_tag()}"}),
        ("DELETE", f"/api/v1/admin/categories/{cat}", None),
    ]:
        r = await api.request(method, path, json=body, headers=who.headers)
        assert r.status_code == 403, f"{role} {method} {path} -> {r.status_code}"


async def test_the_inventory_manager_maintains_the_catalogue(api, world, make_admin):
    inv = await make_admin("inventory_manager", api=api)
    cat = _id(await _category(api, inv), "category_id", "id")
    pid = _id(await _product(api, inv, cat), "product_id", "id")
    r = await api.put(f"/api/v1/admin/products/{pid}", json={"price": "300"}, headers=inv.headers)
    assert r.status_code == 200, r.text
