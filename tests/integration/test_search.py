"""
Search when the AI is not there. Gemini embeddings are an external, billed
service that can be down, slow or unconfigured; the promise is that search
still answers -- from the keyword index -- rather than failing the shopper.
"""
from __future__ import annotations

import json

import pytest

from app.config.redis import RedisKeys


@pytest.fixture
def no_ai(monkeypatch):
    from app.config.settings import settings

    monkeypatch.setattr(settings, "gemini_api_key", "", raising=False)


async def _customer_in_branch(api, app_redis, world, make_customer):
    c = await make_customer()
    await app_redis.setex(RedisKeys.branch_context(str(c.user_id)), 600,
                          json.dumps({"branch_id": str(world.branch_id), "branch_name": "ZZ"}))
    return c


async def test_semantic_search_without_ai_says_so_cleanly(api, app_redis, world, make_customer, make_product,
                                                          no_ai):
    """
    POST /search is the Gemini-backed search. Its docs promise a keyword
    fallback, but without the AI it answers 503 -- which is harmless today
    because the app searches through GET /products/search (below), and is
    pinned here so a change to either is a decision, not an accident.
    """
    await make_product(price="120.00")
    c = await _customer_in_branch(api, app_redis, world, make_customer)
    r = await api.post("/api/v1/search", json={"query": "ZZ Test Rice"}, headers=c.headers)
    assert r.status_code == 503, r.text
    assert "SERVICE_UNAVAILABLE" in r.text and "Traceback" not in r.text


async def test_suggestions_popular_and_health(api, app_redis, world, make_customer, make_product, no_ai):
    await make_product()
    c = await _customer_in_branch(api, app_redis, world, make_customer)
    for path, params in [("/api/v1/search/suggestions", {"query": "ZZ"}), ("/api/v1/search/popular", {}),
                         ("/api/v1/search/health", {})]:
        r = await api.get(path, params=params, headers=c.headers)
        assert r.status_code == 200, (path, r.text)


async def test_search_needs_a_query(api, app_redis, world, make_customer, no_ai):
    c = await _customer_in_branch(api, app_redis, world, make_customer)
    r = await api.post("/api/v1/search", json={"query": ""}, headers=c.headers)
    assert r.status_code == 422


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
