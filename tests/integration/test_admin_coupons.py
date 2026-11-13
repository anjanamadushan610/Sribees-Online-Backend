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


async def _person(db, *, phone=None, email=None, nic=None, provider=None, deleted=False):
    """A customer with a name made of words no other test uses."""
    from app.models.user import User

    word = "Zq" + uuid.uuid4().hex[:6]
    u = User(
        user_id=uuid.uuid4(),
        full_name=f"{word} Perera",
        phone=phone,
        email=email,
        nic=nic,
        auth_provider=provider,
        provider_uid=f"{provider}:{uuid.uuid4().hex}" if provider else None,
        is_verified=bool(email),
        is_phone_verified=bool(phone),
        is_active=True,
        is_blocked=False,
        deleted_at=datetime.utcnow() if deleted else None,
    )
    db.add(u)
    await db.commit()
    return u, word


async def _suggest(api, admin, q):
    r = await api.get(f"{URL}/customer-search", params={"q": q}, headers=admin.headers)
    assert r.status_code == 200, r.text
    return r.json()["data"]["customers"]


async def test_whitelist_search_suggests_from_the_first_letters(api, world, db, make_admin):
    boss = await make_admin("super_admin", api=api)
    phone = _mobile()
    nic = f"{uuid.uuid4().int % 10**9:09d}V"
    email = f"zq.{uuid.uuid4().hex[:8]}@gmail.test"
    u, word = await _person(db, phone=phone, email=email, nic=nic)

    ids = lambda rows: {c["user_id"] for c in rows}  # noqa: E731
    me = str(u.user_id)
    assert me in ids(await _suggest(api, boss, word[:4])), "start of the name"
    assert me in ids(await _suggest(api, boss, word.lower())), "any case"
    assert me in ids(await _suggest(api, boss, "0" + phone[3:7])), "07x... as typed"
    assert me in ids(await _suggest(api, boss, phone[3:])), "without the 0"
    assert me in ids(await _suggest(api, boss, phone[:8])), "+94 form"
    assert me in ids(await _suggest(api, boss, phone[-4:])), "last digits"
    assert me in ids(await _suggest(api, boss, email[:9])), "start of the email"
    assert me in ids(await _suggest(api, boss, nic[:9])), "NIC"
    assert me in ids(await _suggest(api, boss, nic.lower())), "NIC, any case"

    row = next(c for c in await _suggest(api, boss, word) if c["user_id"] == me)
    assert row["phone"] == phone and row["email"] == email
    assert row["customer_code"] and row["customer_code"].startswith("SB-")
    assert "nic" not in row, "the NIC is searched, never sent back"
    assert me in ids(await _suggest(api, boss, row["customer_code"])), "SB number"
    assert me in ids(await _suggest(api, boss, row["customer_code"][3:])), "SB digits alone"

    # A later word of the name counts too.
    two_word, w2 = await _person(db, phone=_mobile())
    two_word.full_name = f"Kasun {w2}"
    await db.commit()
    assert str(two_word.user_id) in ids(await _suggest(api, boss, w2[:5]))

    # A closed account never shows.
    gone, gone_word = await _person(db, phone=_mobile(), deleted=True)
    assert not await _suggest(api, boss, gone_word)
    r = await api.get(f"{URL}/customer-search", params={"q": "%"}, headers=boss.headers)
    assert r.status_code == 200, "LIKE wildcards are matched literally, not as 'everyone'"
    assert not r.json()["data"]["customers"]


async def test_a_branch_marketing_manager_can_find_any_customer(api, world, db, make_admin):
    """New customers have no orders yet; a coupon for them is the point."""
    marketing = await make_admin("marketing_manager", api=api)
    u, word = await _person(db, phone=_mobile())
    assert str(u.user_id) in {c["user_id"] for c in await _suggest(api, marketing, word)}


async def test_whitelist_search_is_for_coupon_staff_only(api, world, make_admin):
    support = await make_admin("customer_support", api=api)
    r = await api.get(f"{URL}/customer-search", params={"q": "a"}, headers=support.headers)
    assert r.status_code == 403
    r = await api.get(f"{URL}/customer-search", params={"q": "a"})
    assert r.status_code in (401, 403)


async def test_a_google_customer_without_a_number_can_be_listed(api, world, db, make_admin):
    boss = await make_admin("super_admin", api=api)
    google, word = await _person(db, email=f"zq.{uuid.uuid4().hex[:8]}@gmail.test", provider="google")
    phoned, _ = await _person(db, phone=_mobile())
    found = await _suggest(api, boss, word)
    assert [c["user_id"] for c in found] == [str(google.user_id)]
    assert found[0]["phone"] is None and found[0]["auth_provider"] == "google"

    r = await _create(api, boss, target_customer_type="specific", whitelisted_customers=[
        {"user_id": str(google.user_id), "name": "G"},
        {"user_id": str(google.user_id)},                       # picked twice
        {"user_id": str(phoned.user_id)},                       # account with a number
        {"phone": "0" + phoned.phone[3:], "name": "typed"},     # same person, typed
    ])
    assert r.status_code == 201, r.text
    listed = r.json()["data"]["whitelisted_customers"]
    assert len(listed) == 2, listed
    by_user = {c["user_id"]: c for c in listed}
    g = by_user[str(google.user_id)]
    assert g["phone"] is None and g["email"].endswith("@gmail.test") and g["customer_code"]
    assert by_user[str(phoned.user_id)]["phone"] == phoned.phone, "the account's own number is listed"

    # Saving again (an edit that resends the list) keeps both.
    cid = r.json()["data"]["coupon_id"]
    r = await api.put(f"{URL}/{cid}", json={"whitelisted_customers": listed}, headers=boss.headers)
    assert r.status_code == 200 and len(r.json()["data"]["whitelisted_customers"]) == 2, r.text

    # A CSV row with only an email finds the account (any case); an unknown
    # email is named back.
    r = await _create(api, boss, target_customer_type="specific", whitelisted_customers=[
        {"name": "From CSV", "email": google.email.upper()},
    ])
    assert r.status_code == 201, r.text
    assert r.json()["data"]["whitelisted_customers"][0]["user_id"] == str(google.user_id)
    r = await _create(api, boss, target_customer_type="specific", whitelisted_customers=[
        {"name": "Ghost", "email": "nobody.zq@nowhere.test"},
    ])
    assert r.status_code == 400 and "Ghost" in r.text and "no customer has that email" in r.text, r.text

    # Neither number nor account, or an account that is gone: named, nothing saved.
    gone, _ = await _person(db, phone=_mobile(), deleted=True)
    r = await _create(api, boss, target_customer_type="specific", whitelisted_customers=[
        {"name": "Nobody"}, {"user_id": str(gone.user_id), "name": "Gone Person"},
    ])
    assert r.status_code == 400
    assert "Nobody (no number)" in r.text and "Gone Person" in r.text, r.text


async def test_deleting_coupons(api, world, make_admin):
    boss = await make_admin("super_admin", api=api)
    cid = (await _create(api, boss)).json()["data"]["coupon_id"]
    r = await api.delete(f"{URL}/{cid}", params={"hard": "true"}, headers=boss.headers)
    assert r.status_code == 200, r.text
    r = await api.delete(f"{URL}/{cid}", headers=boss.headers)
    assert r.status_code == 404
