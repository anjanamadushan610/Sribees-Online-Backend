"""
/p/{id} share page and /.well-known/assetlinks.json.

Unit-level: the DB dependency and product lookup are stubbed, so this runs
without Postgres/Redis.
"""
import io
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
        product_id="9d2b6a3e-1f0c-4c55-9a8e-6f1d2c3b4a59",
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
    # The preview is the branded card, keyed to the primary photo; the page
    # itself shows the plain photo.
    card = share_links._card_key("https://cdn.sribees.lk/a.jpg")
    assert (
        'og:image" content="https://api.sribees.com/p/'
        f'9d2b6a3e-1f0c-4c55-9a8e-6f1d2c3b4a59/share.jpg?v={card}"'
    ) in body
    assert '<img src="https://cdn.sribees.lk/a.jpg"' in body
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


@pytest.mark.asyncio
async def test_preview_urls_keep_https_behind_the_proxy(client, monkeypatch):
    product = SimpleNamespace(
        product_id="p1", name="Tea", price=None,
        images=[SimpleNamespace(image_url="/uploads/products/tea.webp", is_primary=True, sort_order=0)],
    )
    _stub_product(monkeypatch, product)
    client.base_url = httpx.URL("http://api.sribees.com")

    async with client as c:
        r = await c.get("/p/p1", headers={"x-forwarded-proto": "https"})

    assert 'og:image" content="https://api.sribees.com/p/p1/share.jpg?v=' in r.text
    assert 'og:url" content="https://api.sribees.com/p/p1"' in r.text


def _png(size, rgba):
    from PIL import Image

    out = io.BytesIO()
    Image.new("RGBA", size, rgba).save(out, "PNG")
    return out.getvalue()


@pytest.fixture
def media(tmp_path, monkeypatch):
    from app.services.storage.local import LocalDiskStorageProvider

    store = LocalDiskStorageProvider(media_root=str(tmp_path), url_prefix="uploads")
    monkeypatch.setattr(settings, "media_root", str(tmp_path))
    monkeypatch.setattr(share_links, "get_storage", lambda: store)
    return tmp_path


def _product_with(path):
    return SimpleNamespace(
        product_id="p1", name="Tea", price=None,
        images=[SimpleNamespace(image_url=path, is_primary=True, sort_order=0)],
    )


@pytest.mark.asyncio
async def test_share_card_is_drawn_once_then_served_from_disk(client, monkeypatch, media):
    (media / "products").mkdir()
    (media / "products" / "tea.png").write_bytes(_png((600, 300), (20, 120, 40, 255)))
    _stub_product(monkeypatch, _product_with("/uploads/products/tea.png"))
    drawn = []
    real = share_links.product_imagery.render_share_card
    monkeypatch.setattr(
        share_links.product_imagery, "render_share_card",
        lambda data: drawn.append(1) or real(data),
    )

    async with client as c:
        first = await c.get("/p/p1/share.jpg")
        second = await c.get("/p/p1/share.jpg")

    assert first.status_code == second.status_code == 200
    assert first.headers["content-type"] == "image/jpeg"
    assert first.content.startswith(bytes([0xFF, 0xD8]))  # JPEG
    assert second.content == first.content
    assert drawn == [1]
    assert list((media / "share-cards").glob("*.jpg"))


@pytest.mark.asyncio
async def test_share_card_falls_back_to_a_photo_it_cannot_read(client, monkeypatch, media):
    _stub_product(monkeypatch, _product_with("https://cdn.sribees.lk/old.jpg"))

    async with client as c:
        r = await c.get("/p/p1/share.jpg")

    assert r.status_code == 302
    assert r.headers["location"] == "https://cdn.sribees.lk/old.jpg"


@pytest.mark.asyncio
async def test_share_card_404s_without_a_photo(client, monkeypatch, media):
    _stub_product(monkeypatch, SimpleNamespace(product_id="p1", name="Tea", price=None, images=[]))

    async with client as c:
        r = await c.get("/p/p1/share.jpg")

    assert r.status_code == 404
