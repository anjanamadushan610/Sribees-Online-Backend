"""
The SMS page: the Super Admin sees what texts cost and sets the daily total
for the whole server; the sign-in endpoint obeys it at once. Nobody else may
read or change it.
"""
from __future__ import annotations

import uuid

import pytest


def _phone() -> str:
    return "+947" + f"{uuid.uuid4().int % 10**8:08d}"


async def test_the_super_admin_sets_the_daily_total_and_sign_in_obeys_it(api, world, make_admin):
    boss = await make_admin("super_admin", api=api)

    r = await api.get("/api/v1/admin/sms", headers=boss.headers)
    assert r.status_code == 200, r.text
    page = r.json()["data"]
    assert page["provider"] == "console" and page["is_live"] is False
    assert page["rules"]["resend_wait_seconds"] == 300
    assert page["rules"]["per_address_per_hour"] == 10
    assert len(page["history"]) == 7
    assert "api_key" not in r.text.lower()
    before = page["daily_limit"]
    sent = page["sent_today"]

    try:
        r = await api.put("/api/v1/admin/sms/settings", json={"daily_limit": sent + 1}, headers=boss.headers)
        assert r.status_code == 200, r.text
        assert r.json()["data"]["daily_limit"] == sent + 1
        assert r.json()["data"]["remaining_today"] == 1

        # One more text today, then the server stops.
        r = await api.post("/api/v1/auth/phone/request-otp", json={"phone": _phone()},
                           headers={"X-Forwarded-For": "203.0.113.7"})
        assert r.status_code == 200, r.text
        r = await api.post("/api/v1/auth/phone/request-otp", json={"phone": _phone()},
                           headers={"X-Forwarded-For": "203.0.113.8"})
        assert r.status_code == 503, r.text
        assert r.json()["error"]["code"] == "OTP_SMS_UNAVAILABLE"
    finally:
        r = await api.put("/api/v1/admin/sms/settings", json={"daily_limit": before}, headers=boss.headers)
        assert r.status_code == 200, r.text


@pytest.mark.parametrize("value", [-1, 10_001, "lots"])
async def test_the_daily_total_is_validated(api, world, make_admin, value):
    boss = await make_admin("super_admin", api=api)
    r = await api.put("/api/v1/admin/sms/settings", json={"daily_limit": value}, headers=boss.headers)
    assert r.status_code == 422, r.text


@pytest.mark.parametrize("role", ["branch_manager", "marketing_manager", "customer_support"])
async def test_only_the_super_admin(api, world, make_admin, role):
    who = await make_admin(role, api=api)
    assert (await api.get("/api/v1/admin/sms", headers=who.headers)).status_code == 403
    r = await api.put("/api/v1/admin/sms/settings", json={"daily_limit": 5000}, headers=who.headers)
    assert r.status_code == 403
