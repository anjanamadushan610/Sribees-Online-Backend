from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.api.v1 import products


@pytest.fixture
def storefront(monkeypatch):
    rice = SimpleNamespace(category_id=uuid4(), is_active=True)
    category = AsyncMock(return_value=rice)
    shelf = AsyncMock(return_value=SimpleNamespace(is_active=True))
    listing = AsyncMock(return_value=([], 0))
    monkeypatch.setattr(products.CategoryService, "get_by_slug", category)
    monkeypatch.setattr(products.CategoryService, "get_branch_category", shelf)
    monkeypatch.setattr(products.ProductService, "get_all_for_branch", listing)
    return rice, category, shelf, listing


@pytest.mark.asyncio
@pytest.mark.parametrize("query", [" RICE ", "sahal", "hal", "haal", "සහල්", "හාල්"])
async def test_ingredient_query_browses_only_rice_in_the_selected_branch(storefront, query):
    rice, category, shelf, listing = storefront
    db, branch = object(), uuid4()
    assert await products._search_storefront(db, branch, query, 12, 24) == ([], 0)
    category.assert_awaited_once_with(db, "grocery-essentials-rice")
    shelf.assert_awaited_once_with(db, rice.category_id, branch)
    listing.assert_awaited_once_with(
        db, branch_id=branch, limit=12, offset=24, subcategory_id=rice.category_id,
        sort_by="name", sort_order="asc",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("query", ["rice noodles", "red rice", "rice biscuits", "hall"])
async def test_explicit_product_query_uses_normal_search(storefront, query):
    _, category, _, listing = storefront
    db, branch = object(), uuid4()
    await products._search_storefront(db, branch, query, 12, 0)
    category.assert_not_awaited()
    listing.assert_awaited_once_with(db, branch_id=branch, limit=12, offset=0, search=query)


@pytest.mark.asyncio
async def test_hidden_rice_shelf_does_not_fall_back_to_related_products(storefront):
    _, _, shelf, listing = storefront
    shelf.return_value = SimpleNamespace(is_active=False)
    assert await products._search_storefront(object(), uuid4(), "rice", 12, 0) == ([], 0)
    listing.assert_not_awaited()
