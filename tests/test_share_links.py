"""
/p/{id} share page and /.well-known/assetlinks.json.

Unit-level: the DB dependency and product lookup are stubbed, so this runs
without Postgres/Redis.
"""
from decimal import Decimal
from types import SimpleNamespace
from urllib.parse import unquote

import httpx
import pytest

from app.api import share_links
from app.config.database import get_db
from app.config.settings import settings


async def _no_db():
    yield None


@pytest.fixture
def client(monkeypatch):
    from app.main import app

    app.dependency_overrides[get_db] = _no_db
    yield httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://api.sribees.com")
    app.dependency_overrides.pop(get_db, None)


def _stub_product(monkeypatch, product):
    async def lookup(db, identifier, include_inactive=False):
        return product

    monkeypatch.setattr(share_links.ProductService, "get_by_id_or_slug", staticmethod(lookup))


@pytest.mark.asyncio
async def test_share_page_has_preview_and_install_fallback(client, monkeypatch):
    product = SimpleNamespace(
        name='Red Rice <1kg> "Keeri"',
        price=Decimal("1250"),
        images=[
            SimpleNamespace(image_url="https://cdn.sribees.lk/b.jpg", is_primary=False, sort_order=0),
            SimpleNamespace(image_url="https://cdn.sribees.lk/a.jpg", is_primary=True, sort_order=5),
        ],
    )
    _stub_product(monkeypatch, product)

    async with client as c:
        r = await c.get("/p/abc-123")

    assert r.status_code == 200
    body = r.text
    # Name is HTML-escaped, never injected raw.
    assert "Red Rice &lt;1kg&gt; &quot;Keeri&quot;" in body
    assert "<1kg>" not in body
    assert 'og:image" content="https://cdn.sribees.lk/a.jpg"' in body
    assert "Rs. 1,250.00" in body
    assert f"package={settings.android_package_name}" in body
    assert "intent://product/abc-123#Intent;scheme=sribees" in body
    # Install fallback carries the product id through the Play install referrer.
    assert "product_id=abc-123" in unquote(unquote(body))


@pytest.mark.asyncio
async def test_unknown_product_still_redirects_to_store(client, monkeypatch):
    _stub_product(monkeypatch, None)

    async with client as c:
        r = await c.get("/p/does-not-exist")

    assert r.status_code == 200
    assert "play.google.com/store/apps/details" in r.text
    assert "og:image" not in r.text


@pytest.mark.asyncio
async def test_assetlinks_lists_configured_fingerprints(client, monkeypatch):
    monkeypatch.setattr(settings, "android_sha256_cert_fingerprints", "aa:bb , CC:DD")

    async with client as c:
        r = await c.get("/.well-known/assetlinks.json")

    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/json")
    [statement] = r.json()
    assert statement["target"]["package_name"] == settings.android_package_name
    assert statement["target"]["sha256_cert_fingerprints"] == ["AA:BB", "CC:DD"]


@pytest.mark.asyncio
async def test_assetlinks_empty_when_unconfigured(client, monkeypatch):
    monkeypatch.setattr(settings, "android_sha256_cert_fingerprints", "")

    async with client as c:
        r = await c.get("/.well-known/assetlinks.json")

    assert r.json() == []


def _stub_branch(monkeypatch, branch):
    seen = []

    async def lookup(db, branch_id):
        seen.append(branch_id)
        return branch

    monkeypatch.setattr(share_links.branch_service, "get_public_branch", lookup)
    return seen


@pytest.mark.asyncio
async def test_share_page_names_the_sharers_branch_and_carries_it_on(client, monkeypatch):
    from uuid import UUID

    bid = "5b0c1a52-7f3e-4b8e-9a0e-2d1f3c4b5a69"
    _stub_product(monkeypatch, SimpleNamespace(name="Red Rice", price=Decimal("100"), images=[]))
    _stub_branch(monkeypatch, SimpleNamespace(branch_id=UUID(bid), name="Kandy <City>"))

    async with client as c:
        r = await c.get(f"/p/abc-123?b={bid}")

    body = r.text
    assert "Shared from the Kandy &lt;City&gt; branch" in body
    assert f"intent://product/abc-123?b={bid}#Intent" in body
    assert f"branch_id={bid}" in unquote(unquote(body))


@pytest.mark.asyncio
async def test_share_page_ignores_a_branch_it_cannot_vouch_for(client, monkeypatch):
    _stub_product(monkeypatch, SimpleNamespace(name="Red Rice", price=Decimal("100"), images=[]))
    seen = _stub_branch(monkeypatch, None)

    async with client as c:
        bogus = await c.get("/p/abc-123?b=%3Cscript%3E")
        unknown = await c.get("/p/abc-123?b=5b0c1a52-7f3e-4b8e-9a0e-2d1f3c4b5a69")

    for r in (bogus, unknown):
        assert r.status_code == 200
        assert "Shared from" not in r.text
        assert "branch_id" not in unquote(unquote(r.text))
    assert len(seen) == 1  # the non-UUID never reached the database
