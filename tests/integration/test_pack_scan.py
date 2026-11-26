"""
Pack scan (Sribees Supply joint-go-live plan, phase E): the packer scans each unit's QR
label; Online passes it to Supply at once and, when the switch is on, only lets an order
be packed once every unit is scanned. Supply reads the order events to settle its holds.
"""
from __future__ import annotations

import hashlib
import secrets

import pytest
from fastapi import HTTPException

TOKEN = secrets.token_urlsafe(32)


class FakeSupply:
    """Stands in for Sribees Supply's /internal/online/* (a separate service)."""

    def __init__(self):
        self.calls: list[tuple[str, str, dict | None]] = []
        self.scanned: dict[str, list[str]] = {}
        self.refuse: tuple[int, str] | None = None

    async def __call__(self, method, path, json=None):
        self.calls.append((method, path, json))
        if self.refuse:
            raise HTTPException(status_code=self.refuse[0], detail=self.refuse[1])
        if path == "/pick":
            self.scanned.setdefault(json["order_item_id"], []).append(json["label"])
            return {}
        if path == "/unpick":
            for labels in self.scanned.values():
                if json["label"] in labels:
                    labels.remove(json["label"])
            return {}
        return {"items": [{"order_item_id": k, "scanned_units": len(v), "scanned_kg": "0",
                           "labels": v} for k, v in self.scanned.items()]}


@pytest.fixture
def supply(monkeypatch):
    from app.config.settings import settings
    from app.services import supply_pick

    fake = FakeSupply()
    monkeypatch.setattr(settings, "supply_pack_scan_enabled", True)
    monkeypatch.setattr(settings, "supply_api_url", "http://supply.test")
    monkeypatch.setattr(settings, "supply_pick_token", "t")
    monkeypatch.setattr(settings, "finance_outbox_enabled", True)
    monkeypatch.setattr(supply_pick, "_call", fake)
    return fake


async def _order(make_customer, make_product, place_order, qty=2):
    customer = await make_customer()
    product = await make_product(price="300.00", stock=50)
    return await place_order(customer, product, qty)


async def _move(api, admin, oid, status):
    return await api.patch(f"/api/v1/admin/orders/{oid}/status", json={"status": status},
                           headers=admin.headers)


async def _item(api, admin, oid) -> str:
    r = await api.get(f"/api/v1/admin/orders/{oid}", headers=admin.headers)
    items = r.json()["data"]["items"]
    return str(items[0].get("order_item_id") or items[0].get("id"))


async def test_switched_off_packing_works_as_before(
        api, world, make_customer, make_product, place_order, make_admin):
    order = await _order(make_customer, make_product, place_order)
    oid = order["order_id"]
    manager = await make_admin("branch_manager", api=api)
    r = await api.get(f"/api/v1/admin/orders/{oid}/pack-scan", headers=manager.headers)
    assert r.status_code == 200 and r.json()["data"] == {"enabled": False, "complete": True,
                                                         "lines": []}
    r = await api.post(f"/api/v1/admin/orders/{oid}/pack-scan", headers=manager.headers,
                       json={"order_item_id": order["order_id"], "label": "1000001238"})
    assert r.status_code == 409
    for step in ["confirmed", "packing", "packed"]:
        assert (await _move(api, manager, oid, step)).status_code == 200


async def _with_pack_size(db, order) -> None:
    """Give the order's line a pack size (Supply keeps stock per pack size)."""
    import uuid as _uuid

    from sqlalchemy import select

    from app.models.order import OrderItem
    from app.models.product import ProductVariant

    item = (await db.execute(select(OrderItem).where(
        OrderItem.order_id == _uuid.UUID(order["order_id"])))).scalars().first()
    v = ProductVariant(product_id=item.product_id, name="1 pc", sku=f"ZZPK-{_uuid.uuid4().hex[:6]}",
                       price=item.unit_price, is_active=True)
    db.add(v)
    await db.flush()
    item.variant_id = v.variant_id
    await db.commit()


async def test_every_unit_scanned_before_packed(
        api, db, world, make_customer, make_product, place_order, make_admin, supply):
    order = await _order(make_customer, make_product, place_order, qty=2)
    await _with_pack_size(db, order)
    oid = order["order_id"]
    manager = await make_admin("branch_manager", api=api)
    for step in ["confirmed", "packing"]:
        assert (await _move(api, manager, oid, step)).status_code == 200
    item = await _item(api, manager, oid)

    r = await api.post(f"/api/v1/admin/orders/{oid}/pack-scan", headers=manager.headers,
                       json={"order_item_id": item, "label": "1000001238"})
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["enabled"] and not data["complete"] and data["lines"][0]["scanned_units"] == 1
    sent = supply.calls[0][2]
    assert sent["online_order_id"] == oid and sent["order_item_id"] == item
    assert sent["online_branch_id"] == str(world.branch_id) and sent["quantity"] == 2
    assert sent["picked_by"].startswith("zz.branch_manager")

    r = await _move(api, manager, oid, "packed")
    assert r.status_code == 409 and "Scan every unit" in r.json()["detail"]

    # Supply's refusal reaches the packer word for word
    supply.refuse = (422, "label 1000001246 is on Tea, not on this item")
    r = await api.post(f"/api/v1/admin/orders/{oid}/pack-scan", headers=manager.headers,
                       json={"order_item_id": item, "label": "1000001246"})
    assert r.status_code == 422 and "not on this item" in r.json()["detail"]
    supply.refuse = None

    assert (await api.post(f"/api/v1/admin/orders/{oid}/pack-scan", headers=manager.headers,
                           json={"order_item_id": item, "label": "1000001253"})
            ).json()["data"]["complete"]
    # a mistaken scan taken back makes it incomplete again
    r = await api.post(f"/api/v1/admin/orders/{oid}/pack-scan/undo", headers=manager.headers,
                       json={"label": "1000001253"})
    assert r.status_code == 200 and not r.json()["data"]["complete"]
    await api.post(f"/api/v1/admin/orders/{oid}/pack-scan", headers=manager.headers,
                   json={"order_item_id": item, "label": "1000001253"})
    assert (await _move(api, manager, oid, "packed")).status_code == 200
    # a packed order is not scanned any more
    r = await api.post(f"/api/v1/admin/orders/{oid}/pack-scan", headers=manager.headers,
                       json={"order_item_id": item, "label": "1000001261"})
    assert r.status_code == 409


async def test_supply_down_is_a_clear_503(
        api, world, make_customer, make_product, place_order, make_admin, monkeypatch):
    from app.config.settings import settings

    monkeypatch.setattr(settings, "supply_pack_scan_enabled", True)
    monkeypatch.setattr(settings, "supply_api_url", "http://127.0.0.1:9")  # nothing listens
    monkeypatch.setattr(settings, "supply_pick_token", "t")
    monkeypatch.setattr(settings, "finance_outbox_enabled", True)
    order = await _order(make_customer, make_product, place_order)
    manager = await make_admin("branch_manager", api=api)
    r = await api.get(f"/api/v1/admin/orders/{order['order_id']}/pack-scan",
                      headers=manager.headers)
    assert r.status_code == 503 and "not reachable" in r.json()["detail"]


async def test_supply_reads_only_order_events(api, monkeypatch, db):
    from app.config.settings import settings
    from app.models.finance_outbox import FinanceOutbox

    monkeypatch.setattr(settings, "supply_service_token_sha256",
                        hashlib.sha256(TOKEN.encode()).hexdigest())
    monkeypatch.setattr(settings, "supply_allowed_ips", "")
    h = {"Authorization": f"Bearer {TOKEN}"}
    start = (await api.get("/internal/supply/events", params={"after": 10**12}, headers=h))
    assert start.status_code == 200 and start.json()["events"] == []
    from sqlalchemy import func, select
    top = (await db.execute(select(func.coalesce(func.max(FinanceOutbox.seq), 0)))).scalar_one()
    db.add(FinanceOutbox(event_type="order.status", payload={"order_id": "x", "to_status": "delivered"}))
    db.add(FinanceOutbox(event_type="wallet.movement", payload={"amount": "1"}))
    await db.commit()
    r = await api.get("/internal/supply/events", params={"after": top}, headers=h)
    evs = r.json()["events"]
    assert [e["type"] for e in evs] == ["order.status", "wallet.movement"]
    assert evs[0]["payload"]["to_status"] == "delivered" and evs[1]["payload"] == {}
    assert evs[1]["seq"] == evs[0]["seq"] + 1
    assert (await api.get("/internal/supply/events",
                          headers={**h, "X-Forwarded-For": "1.2.3.4"})).status_code == 403
    assert (await api.get("/internal/supply/events")).status_code == 401



async def test_pack_scan_needs_the_order_events(
        api, world, make_customer, make_product, place_order, make_admin, supply, monkeypatch):
    """Review #9: without the outbox Supply never hears of a delivery, so scanning is off."""
    from app.config.settings import settings

    monkeypatch.setattr(settings, "finance_outbox_enabled", False)
    order = await _order(make_customer, make_product, place_order)
    manager = await make_admin("branch_manager", api=api)
    r = await api.get(f"/api/v1/admin/orders/{order['order_id']}/pack-scan", headers=manager.headers)
    assert r.json()["data"]["enabled"] is False



async def test_a_line_without_a_pack_size_needs_no_scan(
        api, world, make_customer, make_product, place_order, make_admin, supply):
    order = await _order(make_customer, make_product, place_order)
    manager = await make_admin("branch_manager", api=api)
    r = await api.get(f"/api/v1/admin/orders/{order['order_id']}/pack-scan", headers=manager.headers)
    data = r.json()["data"]
    assert data["complete"] and data["lines"][0]["no_scan"] is True
