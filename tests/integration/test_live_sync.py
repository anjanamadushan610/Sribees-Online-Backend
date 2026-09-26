"""
Open apps learn what changed from the server instead of re-reading
everything every few minutes (app/services/live_sync.py): a version per
group, one debounced silent push for catalog and banner changes.
"""
from __future__ import annotations

import json
from decimal import Decimal

import pytest
from sqlalchemy import select


@pytest.fixture
def sync_on(monkeypatch, app_redis):
    """Live sync switched on, pushes captured, a short debounce."""
    from app.services import live_sync
    from app.services.fcm_service import FCMService

    live_sync.install()
    monkeypatch.setattr(live_sync, "enabled", True)
    monkeypatch.setattr(live_sync, "PUSH_DEBOUNCE_SECONDS", 0.2)
    pushes = []

    async def fake_send(topic, data):
        pushes.append((topic, data))
        return True

    monkeypatch.setattr(FCMService, "send_data_to_topic", staticmethod(fake_send))
    return pushes


async def _v(redis, scope: str) -> int:
    raw = await redis.get(f"sync:v:{scope}")
    return int(raw or 0)


async def _settled():
    from app.services import live_sync

    await live_sync.drain()


async def _inventory(db, pid):
    from app.models.product import BranchInventory

    return (await db.execute(select(BranchInventory).where(BranchInventory.product_id == pid))).scalar_one()


async def test_price_changes_bump_the_catalog_and_send_one_push(
    db, world, make_product, sync_on, app_redis, monkeypatch
):
    from app.models.product import Product
    from app.services import live_sync

    pid = await make_product(stock=20)
    await _settled()
    sync_on.clear()
    # Wide enough that both commits land inside it on a busy CI runner.
    monkeypatch.setattr(live_sync, "PUSH_DEBOUNCE_SECONDS", 2)
    all_before = await _v(app_redis, "catalog:all")
    branch_before = await _v(app_redis, f"catalog:{world.branch_id}")

    product = await db.get(Product, pid)
    product.price = Decimal("300.00")
    await db.commit()
    inv = await _inventory(db, pid)
    inv.is_on_sale = True
    await db.commit()
    await _settled()

    assert await _v(app_redis, "catalog:all") == all_before + 1
    assert await _v(app_redis, f"catalog:{world.branch_id}") == branch_before + 1
    # Two changes inside the debounce window: one silent push.
    assert len(sync_on) == 1
    topic, data = sync_on[0]
    assert topic == "sync"
    assert data == {"type": "sync", "groups": "catalog", "branches": "all"}


async def test_stock_ticking_down_is_not_news_but_selling_out_is(db, world, make_product, sync_on, app_redis):
    pid = await make_product(stock=20)
    await _settled()
    sync_on.clear()
    scope = f"catalog:{world.branch_id}"
    before = await _v(app_redis, scope)

    inv = await _inventory(db, pid)
    inv.stock_quantity = 19
    await db.commit()
    await _settled()
    assert await _v(app_redis, scope) == before, "20 -> 19 changes nothing a customer sees"
    assert sync_on == []

    inv.reserved_quantity = 19  # nothing left to sell
    await db.commit()
    await _settled()
    assert await _v(app_redis, scope) == before + 1
    assert sync_on[0][1]["branches"] == str(world.branch_id)


async def test_quiet_columns_and_rollbacks_announce_nothing(db, make_product, sync_on, app_redis):
    from app.models.product import Product

    pid = await make_product()
    await _settled()
    before = await _v(app_redis, "catalog:all")

    product = await db.get(Product, pid)
    product.view_count = (product.view_count or 0) + 1
    await db.commit()
    product.price = Decimal("999.00")
    await db.flush()
    await db.rollback()
    await _settled()

    assert await _v(app_redis, "catalog:all") == before


async def test_sync_state_follows_the_customer_and_their_branch(api, db, world, make_customer, sync_on, app_redis):
    from app.config.redis import RedisKeys
    from app.models.notification import Notification

    customer = await make_customer(with_address=False)
    await app_redis.setex(
        RedisKeys.branch_context(str(customer.user_id)), 600,
        json.dumps({"branch_id": str(world.branch_id), "branch_name": "ZZ"}),
    )

    r = await api.get("/api/v1/app/sync-state", headers=customer.headers)
    assert r.status_code == 200, r.text
    first = r.json()["data"]
    assert first["push_topic"] == "sync"
    assert first["catalog"].count(".") == 1 and not first["catalog"].endswith(".-")

    db.add(Notification(user_id=customer.user_id, title="Order shipped", message="On its way"))
    await db.commit()
    await _settled()

    r = await api.get("/api/v1/app/sync-state", headers=customer.headers)
    second = r.json()["data"]
    assert second["user"] != first["user"], "their own notification moves their version"
    assert second["catalog"] == first["catalog"]
    assert sync_on == [], "a customer's own change needs no broadcast"


async def test_a_guest_without_a_location_still_gets_versions(api, sync_on):
    r = await api.get("/api/v1/app/sync-state", headers={"X-Device-Id": "guest_no_location_1"})
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["user"] is None
    assert data["catalog"].endswith(".-")


def test_push_payload_names_the_groups_and_branches():
    from app.services.live_sync import push_payload

    assert push_payload({"catalog:b1", "catalog:b2"}) == {"type": "sync", "groups": "catalog", "branches": "b1,b2"}
    assert push_payload({"banners"}) == {"type": "sync", "groups": "banners", "branches": ""}
    assert push_payload({"catalog:all", "catalog:b1", "banners"})["branches"] == "all"
