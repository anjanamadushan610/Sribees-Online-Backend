"""
The admin coupon screen: who a coupon is for, what it applies to, and every
refusal the form can meet. A coupon is money, so each rule is pinned at the
API as well as in the form.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

URL = "/api/v1/admin/coupons"


def _code(prefix: str = "ZZ") -> str:
    return f"{prefix}{uuid.uuid4().hex[:8].upper()}"


def _window(days_from: int = -1, days_to: int = 30) -> dict:
    now = datetime.now(timezone.utc)
    return {"valid_from": (now + timedelta(days=days_from)).isoformat(),
            "valid_until": (now + timedelta(days=days_to)).isoformat()}


def _mobile() -> str:
    return "+947" + f"{uuid.uuid4().int % 10**8:08d}"


async def _create(api, admin, **over):
    body = {"code": _code(), "description": "ZZ coupon", "discount_type": "percentage",
            "discount_value": "10", **_window(), **over}
    return await api.post(URL, json=body, headers=admin.headers)


async def _tier(api, boss) -> str:
    points = 800000 + uuid.uuid4().int % 90000
    r = await api.post("/api/v1/admin/loyalty/tiers", json={"name": f"ZZ Tier {points}", "min_points": points},
                       headers=boss.headers)
    assert r.status_code == 201, r.text
    return r.json()["data"]["tier_id"]


async def test_a_coupon_for_selected_customers(api, world, make_admin, make_customer):
    boss = await make_admin("super_admin", api=api)
    known = await make_customer()
    stranger = _mobile()

    # The list is checked as a whole: one bad number names itself, nothing saves.
    r = await _create(api, boss, target_customer_type="specific",
                      whitelisted_customers=[{"name": "Nimal", "phone": "12345"}, {"phone": stranger}])
    assert r.status_code == 400 and "Nimal (12345)" in r.text, r.text
    r = await _create(api, boss, target_customer_type="specific", whitelisted_customers=[])
    assert r.status_code == 400, "a whitelist coupon needs at least one customer"

    # Any typed shape is accepted and stored canonically; a known number is
    # tied to its account.
    local = "0" + known.phone[3:]
    r = await _create(api, boss, target_customer_type="specific",
                      whitelisted_customers=[{"name": "Known", "phone": local}, {"phone": stranger},
                                             {"phone": stranger}])
    assert r.status_code == 201, r.text
    coupon = r.json()["data"]
    assert coupon["target_customer_type"] == "specific"
    cid = coupon["coupon_id"]

    # Keeping 'specific' without resending the list keeps the list.
    r = await api.put(f"{URL}/{cid}", json={"description": "renamed", "target_customer_type": "specific"},
                      headers=boss.headers)
    assert r.status_code == 200, r.text
    # Resending just the list (no target) replaces it.
    r = await api.put(f"{URL}/{cid}", json={"whitelisted_customers": [{"phone": _mobile()}]},
                      headers=boss.headers)
    assert r.status_code == 200, r.text
    # Opening it to everyone empties the list, so it cannot come back stale.
    r = await api.put(f"{URL}/{cid}", json={"target_customer_type": "all"}, headers=boss.headers)
    assert r.status_code == 200 and r.json()["data"]["target_customer_type"] == "all"
    r = await api.put(f"{URL}/{cid}", json={"target_customer_type": "specific"}, headers=boss.headers)
    assert r.status_code == 400, "flipping back to 'specific' without a list is refused"


async def test_a_members_only_coupon(api, world, make_admin):
    boss = await make_admin("super_admin", api=api)
    r = await _create(api, boss, min_tier_id=str(uuid.uuid4()))
    assert r.status_code == 400, "the loyalty level must exist"
    r = await _create(api, boss, target_customer_type="tier")
    assert r.status_code == 400, "'tier' needs a level"

    tier = await _tier(api, boss)
    r = await _create(api, boss, min_tier_id=tier)
    assert r.status_code == 201, r.text
    cid = r.json()["data"]["coupon_id"]
    assert r.json()["data"]["target_customer_type"] == "tier", "a level alone makes it members-only"

    r = await api.put(f"{URL}/{cid}", json={"min_tier_id": str(uuid.uuid4())}, headers=boss.headers)
    assert r.status_code == 400
    r = await api.put(f"{URL}/{cid}", json={"target_customer_type": "all"}, headers=boss.headers)
    assert r.status_code == 200 and r.json()["data"]["target_customer_type"] == "all"


async def test_catalog_eligibility_narrows_and_widens(api, world, make_admin, make_product):
    boss = await make_admin("super_admin", api=api)
    pid = await make_product()
    r = await _create(api, boss, product_ids=[str(pid)])
    assert r.status_code == 201, r.text
    cid = r.json()["data"]["coupon_id"]
    r = await api.put(f"{URL}/{cid}", json={"product_ids": [], "category_ids": []}, headers=boss.headers)
    assert r.status_code == 200, r.text


async def test_every_edit_is_checked_against_what_is_stored(api, world, make_admin):
    boss = await make_admin("super_admin", api=api)
    a = (await _create(api, boss)).json()["data"]
    b = (await _create(api, boss, discount_type="fixed", discount_value="100", min_order_value="500")).json()["data"]
    cid = a["coupon_id"]

    cases = [
        ({"code": b["code"]}, "the code is another coupon's"),
        ({"valid_until": (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()}, "ends before it starts"),
        ({"discount_value": "150"}, "a percentage over 100"),
    ]
    for body, why in cases:
        r = await api.put(f"{URL}/{cid}", json=body, headers=boss.headers)
        assert r.status_code == 400, (why, r.text)

    # A fixed discount can never outgrow the order it applies to, whichever
    # side of the pair the edit touches.
    r = await api.put(f"{URL}/{b['coupon_id']}", json={"min_order_value": "50"}, headers=boss.headers)
    assert r.status_code == 400, r.text
    r = await api.put(f"{URL}/{b['coupon_id']}", json={"discount_type": "percentage", "discount_value": "20"},
                      headers=boss.headers)
    assert r.status_code == 200, r.text

    r = await api.put(f"{URL}/{uuid.uuid4()}", json={"description": "x"}, headers=boss.headers)
    assert r.status_code == 404


async def test_list_filters(api, world, make_admin):
    boss = await make_admin("super_admin", api=api)
    live = (await _create(api, boss, branch_id=str(world.branch_id))).json()["data"]
    await _create(api, boss, **_window(days_from=3, days_to=9))
    await _create(api, boss, is_active=False)

    for params in [{"status": "active"}, {"status": "scheduled"}, {"status": "inactive"}, {"status": "expired"},
                   {"is_active": "true"}, {"search": live["code"]}, {"branch_id": "network"},
                   {"branch_id": str(world.branch_id)}, {"branch_id": "not-a-uuid"}, {"page": 2, "limit": 1}]:
        r = await api.get(URL, params=params, headers=boss.headers)
        assert r.status_code == 200, (params, r.text)
    r = await api.get(URL, params={"search": live["code"]}, headers=boss.headers)
    assert [c["code"] for c in r.json()["data"]["coupons"]] == [live["code"]]
    r = await api.get(URL, params={"status": "bogus"}, headers=boss.headers)
    assert r.status_code == 422


async def test_branch_marketing_cannot_touch_network_or_rival_coupons(api, world, make_admin):
    boss = await make_admin("super_admin", api=api)
    marketing = await make_admin("marketing_manager", api=api)
    network = (await _create(api, boss)).json()["data"]
    rival = (await _create(api, boss, branch_id=str(uuid.uuid4()))).json()
    # (a branch that does not exist still pins the row away from this one)
    r = await api.put(f"{URL}/{network['coupon_id']}", json={"description": "x"}, headers=marketing.headers)
    assert r.status_code == 403, "they can see a network-wide coupon but not edit it"
    if "data" in rival:
        r = await api.put(f"{URL}/{rival['data']['coupon_id']}", json={"description": "x"},
                          headers=marketing.headers)
        assert r.status_code == 404, "another branch's coupon does not exist to them"


async def test_deleting_coupons(api, world, make_admin):
    boss = await make_admin("super_admin", api=api)
    cid = (await _create(api, boss)).json()["data"]["coupon_id"]
    r = await api.delete(f"{URL}/{cid}", params={"hard": "true"}, headers=boss.headers)
    assert r.status_code == 200, r.text
    r = await api.delete(f"{URL}/{cid}", headers=boss.headers)
    assert r.status_code == 404
