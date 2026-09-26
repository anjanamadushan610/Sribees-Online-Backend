"""
Storefront search: GET /products/search over the keyword index
(catalog_search_service), which needs no external service to answer.
"""
from __future__ import annotations

import json

import pytest

from app.config.redis import RedisKeys


async def _customer_in_branch(api, app_redis, world, make_customer):
    c = await make_customer()
    await app_redis.setex(RedisKeys.branch_context(str(c.user_id)), 600,
                          json.dumps({"branch_id": str(world.branch_id), "branch_name": "ZZ"}))
    return c


@pytest.mark.parametrize("method, path", [
    ("POST", "/api/v1/search"),
    ("GET", "/api/v1/search/suggestions"),
    ("GET", "/api/v1/search/popular"),
    ("GET", "/api/v1/search/health"),
])
async def test_the_unused_semantic_search_api_is_gone(api, world, make_customer, method, path):
    """
    POST /search (Gemini embeddings + pgvector) and its suggestions, popular
    and health routes were never called by the app or the admin panel, and
    the SQL functions POST /search needed never reached production. The app
    searches through GET /products/search. Removed 2026-09-26.
    """
    c = await make_customer()
    r = await api.request(method, path, json={"query": "rice"} if method == "POST" else None,
                          params={"query": "ri"}, headers=c.headers)
    assert r.status_code == 404, (path, r.status_code, r.text)


async def test_storefront_search_finds_by_name_and_typo(api, app_redis, world, make_customer, make_product, db):
    """The keyword index (catalog_search_service) is the path customers use."""
    from sqlalchemy import update

    from app.models.product import Product
    from app.services.catalog_search_service import reindex_stale

    pid = await make_product()
    await db.execute(update(Product).where(Product.product_id == pid).values(name="ZZ Keeri Samba Rice"))
    await db.commit()
    from app.config.database import async_session_maker

    await reindex_stale(async_session_maker)
    c = await _customer_in_branch(api, app_redis, world, make_customer)
    for q in ("keeri samba", "KEERI", "keeri sambha"):
        r = await api.get("/api/v1/products/search", params={"q": q}, headers=c.headers)
        assert r.status_code == 200, (q, r.text)
    r = await api.get("/api/v1/products/search", params={"q": "keeri samba"}, headers=c.headers)
    assert str(pid) in r.text, "an exact name search finds the product"
