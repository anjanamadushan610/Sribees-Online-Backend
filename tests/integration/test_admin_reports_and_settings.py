"""
The back office: dashboards and analytics, platform settings, the support
desk, and the courier account settings.

Reports are checked for the property that makes a report trustworthy -- two
screens computing the same figure agree -- and for branch scoping: a Branch
Manager's numbers are their branch's, whatever they ask for. Courier API keys
are checked never to come back out in clear.
"""
from __future__ import annotations

import uuid

import pytest

REPORTS = ["summary", "sales", "branches", "top-products", "top-customers", "categories", "order-status"]


async def _delivered_order(api, boss, make_customer, make_product, place_order, qty=2):
    customer = await make_customer()
    order = await place_order(customer, await make_product(price="500.00"), qty)
    r = await api.post(
        f"/api/v1/admin/orders/{order['order_id']}/status/override",
        json={"status": "delivered", "reason": "Delivered and paid in cash at the door."},
        headers=boss.headers,
    )
    assert r.status_code == 200, r.text
    return customer, order


async def test_analytics_agree_with_each_other(api, world, make_admin, make_customer, make_product, place_order):
    boss = await make_admin("super_admin", api=api)
    await _delivered_order(api, boss, make_customer, make_product, place_order)

    data = {}
    for name in REPORTS:
        r = await api.get(f"/api/v1/admin/analytics/{name}", params={"days": 30, "compare": True},
                          headers=boss.headers)
        assert r.status_code == 200, (name, r.text)
        data[name] = r.json()["data"]

    revenue = float(data["summary"]["current"]["revenue"])
    assert revenue > 0
    series = sum(float(p["revenue"]) for p in data["sales"]["series"])
    assert series == pytest.approx(revenue, abs=1.0), "the chart and the KPI must agree"
    branches = sum(float(b.get("revenue") or 0) for b in data["branches"]["branches"])
    branches += float((data["branches"].get("unassigned") or {}).get("revenue") or 0)
    assert branches == pytest.approx(revenue, abs=1.0), "branch rows must sum to the total"
    assert int(data["order-status"]["total_orders"]) == int(data["summary"]["current"]["orders"])


async def test_a_branch_managers_reports_are_their_branchs(api, world, make_admin, make_customer, make_product,
                                                           place_order):
    boss = await make_admin("super_admin", api=api)
    await _delivered_order(api, boss, make_customer, make_product, place_order)
    manager = await make_admin("branch_manager", api=api)

    r = await api.get("/api/v1/admin/analytics/branches", headers=manager.headers)
    assert r.status_code == 200, r.text
    ids = {b.get("branch_id") for b in r.json()["data"]["branches"]}
    assert ids <= {str(world.branch_id)}, f"a manager sees only their branch, saw {ids}"

    # Asking for another branch changes nothing: scope comes from the token.
    r = await api.get("/api/v1/admin/analytics/summary", params={"branch_id": str(uuid.uuid4())},
                      headers=manager.headers)
    assert r.status_code == 200
    mine = await api.get("/api/v1/admin/analytics/summary", headers=manager.headers)
    assert r.json()["data"]["current"] == mine.json()["data"]["current"]

    marketing = await make_admin("marketing_manager", api=api)
    assert (await api.get("/api/v1/admin/analytics/summary", headers=marketing.headers)).status_code == 403


@pytest.mark.parametrize("role", ["super_admin", "branch_manager", "marketing_manager", "inventory_manager",
                                  "customer_support"])
async def test_every_role_has_a_dashboard(api, world, make_admin, role):
    who = await make_admin(role, api=api)
    r = await api.get("/api/v1/admin/dashboard/stats", headers=who.headers)
    assert r.status_code == 200, (role, r.text)


# ---------------------------------------------------------------------------
# Platform settings
# ---------------------------------------------------------------------------


async def test_platform_settings(api, world, make_admin):
    boss = await make_admin("super_admin", api=api)
    r = await api.get("/api/v1/admin/settings", headers=boss.headers)
    assert r.status_code == 200, r.text
    before = r.json()["data"]

    r = await api.patch("/api/v1/admin/settings", json={"order_tax_rate_percent": 0, "loyalty_spend_per_point": 1000},
                        headers=boss.headers)
    assert r.status_code == 200, r.text
    r = await api.patch("/api/v1/admin/settings", json={"order_tax_rate_percent": 150}, headers=boss.headers)
    assert r.status_code == 422, "a tax rate above 100% is refused"

    for path in ["/api/v1/admin/settings/splash-video", "/api/v1/admin/settings/splash-animation"]:
        r = await api.get(path, headers=boss.headers)
        assert r.status_code == 200, (path, r.text)

    r = await api.post("/api/v1/admin/settings/splash-animation",
                       files={"file": ("x.json", b"not json at all", "application/json")}, headers=boss.headers)
    assert r.status_code == 400, "a broken animation file is refused, not stored"

    manager = await make_admin("branch_manager", api=api)
    r = await api.patch("/api/v1/admin/settings", json={"order_tax_rate_percent": 0}, headers=manager.headers)
    assert r.status_code == 403
    assert before is not None


# ---------------------------------------------------------------------------
# Support desk
# ---------------------------------------------------------------------------


async def test_the_support_desk(api, world, make_customer, make_admin):
    customer = await make_customer()
    r = await api.post(
        "/api/v1/support/tickets",
        json={"contact_phone": customer.phone, "category": "order", "message": "My rice has not arrived yet"},
        headers=customer.headers,
    )
    assert r.status_code == 201, r.text
    ticket_id = r.json()["ticket_id"]

    r = await api.get("/api/v1/support/my-tickets", headers=customer.headers)
    assert ticket_id in r.text

    support = await make_admin("customer_support", branch_id=None, api=api)
    r = await api.get("/api/v1/admin/support/tickets", params={"search": customer.phone[-9:]},
                      headers=support.headers)
    assert r.status_code == 200 and ticket_id in r.text
    r = await api.get(f"/api/v1/admin/support/tickets/{ticket_id}", headers=support.headers)
    assert r.status_code == 200, r.text
    r = await api.patch(f"/api/v1/admin/support/tickets/{ticket_id}",
                        json={"status": "resolved", "resolution_notes": "Rider delivered at 4pm"},
                        headers=support.headers)
    assert r.status_code == 200, r.text

    r = await api.get("/api/v1/support/my-tickets", headers=customer.headers)
    assert "resolved" in r.text


async def test_a_guest_sees_only_tickets_from_their_own_device(api, world):
    phone = "+947" + f"{uuid.uuid4().int % 10**8:08d}"
    mine = {"X-Device-Id": f"zz-dev-{uuid.uuid4().hex[:8]}"}
    r = await api.post("/api/v1/support/tickets",
                       json={"contact_phone": phone, "category": "general", "message": "Where is my order please"},
                       headers=mine)
    assert r.status_code == 201, r.text
    tid = r.json()["ticket_id"]

    r = await api.get("/api/v1/support/my-tickets", params={"phone": phone}, headers=mine)
    assert tid in r.text

    # Any contact number: a second ticket from the same install with another
    # number keeps the first one in view, with or without ?phone=.
    other = "0" + f"{uuid.uuid4().int % 10**9:09d}"
    r = await api.post("/api/v1/support/tickets",
                       json={"contact_phone": other, "category": "general", "message": "Please call my wife instead"},
                       headers=mine)
    assert r.status_code == 201, r.text
    tid2 = r.json()["ticket_id"]
    for params in ({}, {"phone": other}, {"phone": phone}):
        r = await api.get("/api/v1/support/my-tickets", params=params, headers=mine)
        assert tid in r.text and tid2 in r.text, params
    # Same phone number, another install: nothing. A phone number is not proof.
    r = await api.get("/api/v1/support/my-tickets", params={"phone": phone},
                      headers={"X-Device-Id": "zz-attacker-device"})
    assert tid not in r.text
    r = await api.get("/api/v1/support/my-tickets", params={"phone": phone})
    assert tid not in r.text


# ---------------------------------------------------------------------------
# Courier account settings
# ---------------------------------------------------------------------------


async def test_courier_keys_never_come_back_in_clear(api, world, make_admin):
    boss = await make_admin("super_admin", api=api)
    secret = f"sk_test_{uuid.uuid4().hex}"
    r = await api.put("/api/v1/admin/courier/credentials/key", json={"environment": "test", "api_key": secret},
                      headers=boss.headers)
    assert r.status_code == 200, r.text
    assert secret not in r.text, "a saved key is returned masked"

    r = await api.get("/api/v1/admin/courier/credentials", headers=boss.headers)
    assert r.status_code == 200 and secret not in r.text

    r = await api.put("/api/v1/admin/courier/credentials/base-url",
                      json={"environment": "test", "base_url": "https://courier.example.test"}, headers=boss.headers)
    assert r.status_code == 200, r.text
    r = await api.put(f"/api/v1/admin/courier/credentials/branches/{world.branch_id}/key",
                      json={"environment": "test", "api_key": f"sk_test_{uuid.uuid4().hex}"}, headers=boss.headers)
    assert r.status_code == 200, r.text

    manager = await make_admin("branch_manager", api=api)
    r = await api.put("/api/v1/admin/courier/credentials/key", json={"environment": "live", "api_key": "sk_live_x"},
                      headers=manager.headers)
    assert r.status_code == 403, "only the Super Admin touches the courier account"


async def test_courier_reads_through_the_fake_courier(api, world, make_admin, fake_courier):
    boss = await make_admin("super_admin", api=api)
    for path in ["/api/v1/admin/courier/cod-balance", "/api/v1/admin/courier/remittances"]:
        r = await api.get(path, headers=boss.headers)
        assert r.status_code == 200, (path, r.text)
