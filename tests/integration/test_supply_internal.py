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
                        "display_size_unit", "category", "is_active", "image_url", "variants", "weight_grams",
                        "listings"}


async def test_catalog_has_the_main_photo(api, db, make_product):
    """Owner 2026-11-26: Supply's matching screen shows the product's photo."""
    from app.models.product import ProductImage

    pid = await make_product()
    db.add(ProductImage(product_id=pid, image_url="/uploads/products/side.webp", sort_order=0))
    db.add(ProductImage(product_id=pid, image_url="/uploads/products/main.webp",
                        is_primary=True, sort_order=1))
    await db.commit()
    r = await api.get("/internal/supply/catalog", headers={"Authorization": f"Bearer {TOKEN}"})
    row = next(p for p in r.json()["products"] if p["product_id"] == str(pid))
    assert row["image_url"].endswith("/uploads/products/main.webp")


async def test_catalog_lists_the_branches(api, test_branch):
    """Sribees Supply follows our branches from here: id, code, name, active; nothing else."""
    r = await api.get("/internal/supply/catalog", headers={"Authorization": f"Bearer {TOKEN}"})
    assert r.status_code == 200, r.text
    row = next(b for b in r.json()["branches"] if b["branch_id"] == str(test_branch.branch_id))
    assert row == {"branch_id": str(test_branch.branch_id), "code": test_branch.code,
                   "name": test_branch.name, "is_active": True}


# ---------------------------------------------------------------- fulfilment partner
async def test_catalog_lists_where_each_product_is_sold_and_how(api, db, make_product, world):
    """Owner 2026-11-27: Supply sends a branch's "Other Products" lines to its partner."""
    from sqlalchemy import select

    from app.models.product import BranchInventory

    store = await make_product()
    other = await make_product()
    hidden = await make_product()
    rows = {r.product_id: r for r in (await db.execute(select(BranchInventory).where(
        BranchInventory.product_id.in_([store, other, hidden])))).scalars()}
    rows[store].sourcing_group = "store"
    rows[other].sourcing_group = "external_daily"
    rows[hidden].is_active = False
    await db.commit()
    r = await api.get("/internal/supply/catalog", headers={"Authorization": f"Bearer {TOKEN}"})
    by_id = {p["product_id"]: p for p in r.json()["products"]}
    assert by_id[str(store)]["listings"] == [
        {"branch_id": str(world.branch_id), "sourcing_group": "store"}]
    assert by_id[str(other)]["listings"] == [
        {"branch_id": str(world.branch_id), "sourcing_group": "external_daily"}]
    assert by_id[str(hidden)]["listings"] == []  # not on sale there


async def test_order_delivery_for_the_partner_app(api, make_product, make_customer, place_order):
    pid = await make_product()
    customer = await make_customer()
    order = await place_order(customer, pid, qty=2)
    url = f"/internal/supply/orders/{order['order_id']}"
    assert (await api.get(url)).status_code == 401
    r = await api.get(url, headers={"Authorization": f"Bearer {TOKEN}"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["order_number"] == order["order_number"]
    assert body["customer_name"] == "ZZ Customer"
    assert body["phone"] == customer.phone and body["alternate_phone"] == "+94719999999"
    assert "12 Test Lane" in body["address"]
    assert [(ln["product_id"], ln["quantity"]) for ln in body["lines"]] == [(str(pid), 2)]
    missing = await api.get("/internal/supply/orders/00000000-0000-0000-0000-000000000001",
                            headers={"Authorization": f"Bearer {TOKEN}"})
    assert missing.status_code == 404


async def test_order_events_carry_lines_for_supply(api, db, monkeypatch, make_product,
                                                   make_customer, place_order, world):
    """Supply learns of a new order from its first order.status event: it carries the
    branch but NOT the lines (written before they exist), so Supply asks /orders/{id}."""
    from sqlalchemy import select

    from app.config.settings import settings
    from app.models.finance_outbox import FinanceOutbox

    monkeypatch.setattr(settings, "finance_outbox_enabled", True)
    pid = await make_product()
    order = await place_order(await make_customer(), pid, qty=3)
    rows = (await db.execute(select(FinanceOutbox).where(
        FinanceOutbox.aggregate_id == order["order_id"],
        FinanceOutbox.event_type == "order.status").order_by(FinanceOutbox.seq))).scalars().all()
    assert rows, "a new order writes an order.status event"
    first = rows[0].payload
    assert first["from_status"] is None and first["branch"]["branch_id"] == str(world.branch_id)
    assert first["lines"] == []
    detail = await api.get(f"/internal/supply/orders/{order['order_id']}",
                           headers={"Authorization": f"Bearer {TOKEN}"})
    assert detail.json()["lines"][0]["quantity"] == 3
