"""
Partial delivery (app/services/partial_delivery.py): SribeesExpress reports a
COD order `delivered` with less collected than its total. Once the refused items
are back, staff record them: stock back on the shelf, a Finance event, no refund.
"""
from __future__ import annotations

import hashlib
import secrets
from decimal import Decimal

import pytest
from sqlalchemy import select

from tests.integration.test_order_lifecycle import (  # noqa: F401
    _dispatched,
    _push,
    _webhook_secret,
)

TOKEN = secrets.token_urlsafe(32)


@pytest.fixture(autouse=True)
def _finance_on(monkeypatch):
    from app.config.settings import settings

    monkeypatch.setattr(settings, "finance_outbox_enabled", True)
    monkeypatch.setattr(settings, "finance_service_token_sha256", hashlib.sha256(TOKEN.encode()).hexdigest())
    monkeypatch.setattr(settings, "finance_allowed_ips", "")


async def _shelf(db, world, product) -> int:
    from app.models.product import BranchInventory

    db.expire_all()
    return (await db.execute(select(BranchInventory.stock_quantity).where(
        BranchInventory.branch_id == world.branch_id, BranchInventory.product_id == product))).scalar_one()


async def test_refused_items_come_back_on_the_shelf_and_to_finance(
    api, db, world, make_customer, make_product, place_order, make_admin, fake_courier
):
    _, order, sid, manager, product = await _dispatched(
        api, make_customer, make_product, place_order, make_admin, fake_courier, qty=2)
    total = Decimal(str(order["total_amount"]))
    # One of the two Rs 400 items refused at the door: Rs 400 less collected.
    r, _ = await _push(api, sid, "delivered", cod={"collected": True, "amount": str(total - 400)})
    assert r.status_code == 200, r.text
    oid = order["order_id"]
    shelf = await _shelf(db, world, product)

    r = await api.get(f"/api/v1/admin/orders/{oid}", headers=manager.headers)
    item = r.json()["data"]["items"][0]
    item_id = item.get("order_item_id") or item.get("id")
    r = await api.post(f"/api/v1/admin/orders/{oid}/undelivered-items", headers=manager.headers,
                       json={"items": [{"order_item_id": item_id, "quantity": 3}]})
    assert r.status_code == 409, "more came back than was ordered"
    r = await api.post(f"/api/v1/admin/orders/{oid}/undelivered-items", headers=manager.headers,
                       json={"items": [{"order_item_id": item_id, "quantity": 1}]})
    assert r.status_code == 200, r.text
    block = r.json()["data"]["courier"]
    assert block["cod_shortfall"] == 400.0
    assert block["undelivered_items"] == [{"order_item_id": item_id, "quantity": 1}]
    assert await _shelf(db, world, product) == shelf + 1, "the refused item is back on the shelf"

    # Recorded once.
    r = await api.post(f"/api/v1/admin/orders/{oid}/undelivered-items", headers=manager.headers,
                       json={"items": [{"order_item_id": item_id, "quantity": 1}]})
    assert r.status_code == 409

    events = (await api.get("/internal/finance/events", params={"limit": 2000},
                            headers={"Authorization": f"Bearer {TOKEN}"})).json()["events"]
    mine = [e for e in events if e["aggregate_id"] == oid]
    delivered = [e for e in mine if e["type"] == "order.status" and e["payload"]["to_status"] == "delivered"]
    assert delivered and delivered[-1]["payload"]["amounts"]["cod_collected"] == f"{total - 400:.2f}"
    [back] = [e for e in mine if e["type"] == "order.undelivered"]
    assert back["payload"]["undelivered"] == [{"order_item_id": item_id, "quantity": 1}]


async def test_finance_reads_express_remittances_order_by_order(
    api, db, world, make_customer, make_product, place_order, make_admin, fake_courier, monkeypatch
):
    from app.services.courier_service import CourierService

    _, order, sid, _, _ = await _dispatched(
        api, make_customer, make_product, place_order, make_admin, fake_courier, qty=1)

    async def runs(*, limit=50, offset=0):
        return [{"id": 7, "gross_cod_amount": "1150.00", "deductions_amount": "350.00",
                 "net_amount": "800.00", "shipment_count": 1, "status": "paid",
                 "bank_reference": "T-7", "created_at": "2026-11-10T09:00:00+00:00",
                 "paid_at": "2026-11-11T09:00:00+00:00"}]

    async def shipments(remittance_id):
        assert remittance_id == 7
        return [{"shipment_id": sid, "merchant_order_ref": order["order_number"],
                 "amount_lkr": "350.00", "cod": {"collected": True, "amount": 1150.0}},
                {"shipment_id": "SX-NOT-OURS", "merchant_order_ref": None, "amount_lkr": "300.00",
                 "cod": {"collected": True, "amount": 0}}]

    async def balance():
        return {"collected_not_remitted": "2400.00", "shipment_count": 2,
                "oldest_collection_at": "2026-11-12T08:00:00+00:00"}

    monkeypatch.setattr(CourierService, "list_remittances", staticmethod(runs))
    monkeypatch.setattr(CourierService, "get_remittance_shipments", staticmethod(shipments))
    monkeypatch.setattr(CourierService, "get_cod_balance", staticmethod(balance))

    r = await api.get("/internal/finance/cod-remittances", headers={"Authorization": f"Bearer {TOKEN}"})
    assert r.status_code == 200, r.text
    [run] = r.json()["remittances"]
    assert (run["remittance_id"], run["net"], run["deductions"]) == (7, "800.00", "350.00")
    ours, stranger = run["shipments"]
    assert ours["order_id"] == order["order_id"] and ours["collected"] == "1150.0"
    assert stranger["order_id"] is None
    assert r.json()["express_balance"]["collected_not_remitted"] == "2400.00"
    assert (await api.get("/internal/finance/cod-remittances")).status_code == 401


async def test_a_fully_collected_order_has_nothing_undelivered(
    api, world, make_customer, make_product, place_order, make_admin, fake_courier
):
    _, order, sid, manager, _ = await _dispatched(
        api, make_customer, make_product, place_order, make_admin, fake_courier, qty=1)
    r, _ = await _push(api, sid, "delivered", cod={"collected": True, "amount": str(order["total_amount"])})
    assert r.status_code == 200, r.text
    r = await api.get(f"/api/v1/admin/orders/{order['order_id']}", headers=manager.headers)
    item = r.json()["data"]["items"][0]
    r = await api.post(f"/api/v1/admin/orders/{order['order_id']}/undelivered-items", headers=manager.headers,
                       json={"items": [{"order_item_id": item.get("order_item_id") or item.get("id"),
                                        "quantity": 1}]})
    assert r.status_code == 409 and "whole amount" in r.json()["detail"]
