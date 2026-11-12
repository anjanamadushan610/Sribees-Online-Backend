"""
The product list Sribees Supply mirrors: locked like /internal/finance/*, read only,
and carries the Sinhala name its supplier matching depends on.
"""
from __future__ import annotations

import hashlib
import secrets

import pytest

TOKEN = secrets.token_urlsafe(32)


@pytest.fixture(autouse=True)
def _supply_on(monkeypatch):
    from app.config.settings import settings

    monkeypatch.setattr(settings, "supply_service_token_sha256", hashlib.sha256(TOKEN.encode()).hexdigest())
    monkeypatch.setattr(settings, "supply_allowed_ips", "")


async def test_the_catalog_is_locked(api, monkeypatch):
    from app.config.settings import settings

    assert (await api.get("/internal/supply/catalog")).status_code == 401
    wrong = await api.get("/internal/supply/catalog", headers={"Authorization": "Bearer nope"})
    assert wrong.status_code == 401
    edge = await api.get("/internal/supply/catalog", headers={
        "Authorization": f"Bearer {TOKEN}", "X-Forwarded-For": "203.0.113.9"})
    assert edge.status_code == 403, "a request that came through the edge proxy is refused"
    monkeypatch.setattr(settings, "supply_allowed_ips", "10.99.0.0/16")
    far = await api.get("/internal/supply/catalog", headers={"Authorization": f"Bearer {TOKEN}"})
    assert far.status_code == 403
    monkeypatch.setattr(settings, "supply_service_token_sha256", "")
    assert (await api.get("/internal/supply/catalog",
                          headers={"Authorization": f"Bearer {TOKEN}"})).status_code == 404


async def test_finance_token_does_not_open_supply(api, monkeypatch):
    from app.config.settings import settings

    other = secrets.token_urlsafe(32)
    monkeypatch.setattr(settings, "finance_service_token_sha256", hashlib.sha256(other.encode()).hexdigest())
    r = await api.get("/internal/supply/catalog", headers={"Authorization": f"Bearer {other}"})
    assert r.status_code == 401


async def test_catalog_has_sinhala_name_and_size(api, db, make_product):
    from app.models.product import Product

    pid = await make_product()
    product = await db.get(Product, pid)
    product.name_si = "බේබි ශෙරමි සබන්"
    product.display_size, product.display_size_unit = "75", "g"
    await db.commit()

    r = await api.get("/internal/supply/catalog", headers={"Authorization": f"Bearer {TOKEN}"})
    assert r.status_code == 200, r.text
    row = next(p for p in r.json()["products"] if p["product_id"] == str(pid))
    assert row["name_si"] == "බේබි ශෙරමි සබන්"
    assert (row["display_size"], row["display_size_unit"]) == ("75", "g")
    assert row["is_active"] is True and row["variants"] == []
    assert set(row) == {"product_id", "sku", "name", "name_si", "display_size",
                        "display_size_unit", "category", "is_active", "variants"}
