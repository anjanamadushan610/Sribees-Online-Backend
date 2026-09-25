"""
Money that moves after delivery: a customer's cashback and loyalty points, and
the commission their referrer earns -- both held until the return window
closes, then settled by background sweeps.

The sweeps are the real ones (order_reward_worker, referral_vesting_worker).
"Time passing" is done by moving the hold's end into the past in the
database, never by sleeping, so the tests are exact and fast.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select, update


def _data(r):
    body = r.json()
    return body.get("data", body)


def _balance(r) -> float:
    return float(_data(r)["balance"])


async def _deliver(api, boss, order_id):
    r = await api.post(
        f"/api/v1/admin/orders/{order_id}/status/override",
        json={"status": "delivered", "reason": "Delivered and cash collected at the door."},
        headers=boss.headers,
    )
    assert r.status_code == 200, r.text


async def _code_of(api, customer) -> str:
    r = await api.get("/api/v1/referrals/me", headers=customer.headers)
    assert r.status_code == 200, r.text
    d = _data(r)
    code = d.get("referral_code") or d.get("referralCode") or d.get("code")
    assert code, d
    return code


async def _redeem(api, customer, code):
    return await api.post("/api/v1/referrals/redeem", json={"referral_code": code}, headers=customer.headers)


async def test_invite_codes_are_write_once_and_not_self_serve(api, world, make_customer):
    alice = await make_customer()
    bob = await make_customer()
    code = await _code_of(api, alice)

    assert (await _redeem(api, alice, code)).status_code == 400, "no one refers themselves"
    bad = await _redeem(api, bob, "NOSUCH999")
    assert bad.status_code == 400
    r = await _redeem(api, bob, code.lower())
    assert r.status_code == 200, r.text
    again = await _redeem(api, bob, code)
    assert again.status_code == 400, "an invite code binds once"
    # A wrong code and an unknown one read the same: no code enumeration.
    carol = await make_customer()
    assert (await _redeem(api, carol, "ZZZZZZZZ")).json() == bad.json()


async def test_a_referrer_is_paid_once_the_return_window_closes(
    api, db, world, make_customer, make_product, place_order, make_admin
):
    from app.models.referral import ReferralCommission
    from app.services import referral_vesting_worker

    alice, bob = await make_customer(), await make_customer()
    assert (await _redeem(api, bob, await _code_of(api, alice))).status_code == 200
    order = await place_order(bob, await make_product(price="1000.00"), 2)
    boss = await make_admin("super_admin", api=api)
    await _deliver(api, boss, order["order_id"])

    db.expire_all()
    rows = (await db.execute(select(ReferralCommission).where(
        ReferralCommission.order_id == order["order_id"]))).scalars().all()
    assert len(rows) == 1 and rows[0].beneficiary_user_id == alice.user_id
    assert float(rows[0].amount) > 0
    earned = float(rows[0].amount)

    # Nothing to claim yet: the goods could still come back.
    r = await api.post("/api/v1/referrals/claim", headers=alice.headers)
    assert r.status_code == 200, r.text
    assert _balance(await api.get("/api/v1/wallet", headers=alice.headers)) == 0.0

    await db.execute(update(ReferralCommission).where(ReferralCommission.order_id == order["order_id"])
                     .values(claimable_at=datetime.now(timezone.utc) - timedelta(minutes=1)))
    await db.commit()
    assert await referral_vesting_worker.sweep_once() >= 1

    r = await api.post("/api/v1/referrals/claim", headers=alice.headers)
    assert r.status_code == 200, r.text
    assert _balance(await api.get("/api/v1/wallet", headers=alice.headers)) == pytest.approx(earned)
    # Claiming again pays nothing more.
    await api.post("/api/v1/referrals/claim", headers=alice.headers)
    assert _balance(await api.get("/api/v1/wallet", headers=alice.headers)) == pytest.approx(earned)

    r = await api.get(f"/api/v1/referrals/friends/{bob.user_id}", headers=alice.headers)
    assert r.status_code == 200, r.text


async def test_a_cancelled_order_earns_the_referrer_nothing(
    api, db, world, make_customer, make_product, place_order
):
    from app.models.referral import ReferralCommission

    alice, bob = await make_customer(), await make_customer()
    await _redeem(api, bob, await _code_of(api, alice))
    order = await place_order(bob, await make_product(price="800.00"), 1)
    r = await api.post(f"/api/v1/orders/{order['order_id']}/cancel", headers=bob.headers)
    assert r.status_code == 200, r.text
    db.expire_all()
    live = (await db.execute(select(ReferralCommission).where(
        ReferralCommission.order_id == order["order_id"],
        ReferralCommission.status.in_(["pending", "claimable"])))).scalars().all()
    assert live == []


async def test_cashback_and_points_wait_for_the_return_window(
    api, db, world, make_customer, make_product, place_order, make_admin
):
    from app.models.loyalty import OrderReward
    from app.services import order_reward_worker

    customer = await make_customer()
    order = await place_order(customer, await make_product(price="2500.00"), 2)
    boss = await make_admin("super_admin", api=api)
    await _deliver(api, boss, order["order_id"])

    wallet = _data(await api.get("/api/v1/wallet", headers=customer.headers))
    assert float(wallet["balance"]) == 0.0, "cashback is held, not paid, at delivery"
    assert float(wallet.get("pending_cashback", 0)) > 0

    await db.execute(update(OrderReward).where(OrderReward.order_id == order["order_id"])
                     .values(vests_at=datetime.now(timezone.utc) - timedelta(minutes=1)))
    await db.commit()
    assert await order_reward_worker.sweep_once() >= 1

    db.expire_all()
    reward = (await db.execute(select(OrderReward).where(OrderReward.order_id == order["order_id"]))).scalar_one()
    assert reward.status == "settled"
    assert _balance(await api.get("/api/v1/wallet", headers=customer.headers)) == pytest.approx(
        float(reward.cashback_amount))
    # 1 point per Rs 1,000 spent, delivery excluded: 5,000 of goods is 5 points.
    assert reward.points_awarded == 5

    # Settled is final: a second sweep pays nothing more.
    before = _balance(await api.get("/api/v1/wallet", headers=customer.headers))
    await order_reward_worker.sweep_once()
    assert _balance(await api.get("/api/v1/wallet", headers=customer.headers)) == pytest.approx(before)


def _partner_pw() -> str:
    import secrets

    return f"Zz{secrets.token_urlsafe(10)}9!"


async def _partner_login(api, email, password) -> dict:
    import uuid as _uuid

    r = await api.post("/api/v1/partner/auth/login", json={"email": email, "password": password},
                       headers={"X-Forwarded-For": f"192.0.2.{_uuid.uuid4().int % 250}"})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['data']['token']}"}


async def test_partner_commissions_pay_two_levels(
    api, db, world, make_customer, make_product, place_order, make_admin
):
    """An agent's customer buys: the agent earns level 1, the lead who recruited
    them earns level 2 -- and nobody further up."""
    import uuid as _uuid

    from app.models.referral import ReferralCommission

    boss = await make_admin("super_admin", api=api)
    lead_email, lead_pw = f"zz.lead.{_uuid.uuid4().hex[:8]}@sribees-ci.lk", _partner_pw()
    r = await api.post("/api/v1/admin/partners",
                       json={"email": lead_email, "password": lead_pw, "full_name": "ZZ Lead"},
                       headers=boss.headers)
    assert r.status_code == 201, r.text
    lead = await _partner_login(api, lead_email, lead_pw)

    agent_email, agent_pw = f"zz.agent.{_uuid.uuid4().hex[:8]}@sribees-ci.lk", _partner_pw()
    r = await api.post("/api/v1/partner/recruits",
                       json={"email": agent_email, "password": agent_pw, "full_name": "ZZ Agent"}, headers=lead)
    assert r.status_code == 201, r.text
    agent = await _partner_login(api, agent_email, agent_pw)
    agent_code = (await api.get("/api/v1/partner/me", headers=agent)).json()["data"]["referralCode"]

    buyer = await make_customer()
    assert (await _redeem(api, buyer, agent_code)).status_code == 200
    order = await place_order(buyer, await make_product(price="1000.00"), 5)
    await _deliver(api, boss, order["order_id"])

    db.expire_all()
    rows = (await db.execute(select(ReferralCommission).where(
        ReferralCommission.order_id == order["order_id"]))).scalars().all()
    by_level = {r.level: r for r in rows}
    assert set(by_level) == {1, 2}, "two levels, no more"
    assert float(by_level[1].amount) == pytest.approx(5000 * 0.02)
    assert float(by_level[2].amount) == pytest.approx(5000 * 0.01)
    assert by_level[1].beneficiary_user_id is None and by_level[1].beneficiary_partner_id is not None

    r = await api.get("/api/v1/partner/commissions", headers=agent)
    assert r.status_code == 200 and order["order_number"] in r.text
    r = await api.get("/api/v1/partner/customers", headers=agent)
    assert r.status_code == 200
