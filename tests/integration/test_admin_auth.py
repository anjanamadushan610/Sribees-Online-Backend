"""
Admin sign-in beyond the login form: token refresh, profile, changing a
password, and logging out. An admin token opens the whole back office, so each
check is also about what an old or stolen token can still do.
"""
from __future__ import annotations

import secrets
import uuid


def _pw() -> str:
    return f"Zz{secrets.token_urlsafe(10)}9!"


def _ip() -> dict:
    return {"X-Forwarded-For": f"198.18.{uuid.uuid4().int % 250}.{uuid.uuid4().int % 250}"}


async def _login(api, email, password):
    return await api.post("/api/v1/admin/auth/login", json={"email": email, "password": password}, headers=_ip())


async def test_refresh_profile_and_logout(api, world, make_admin):
    admin = await make_admin("branch_manager", api=api)
    r = await _login(api, admin.email, admin.password)
    data = r.json()["data"]
    refresh = data.get("refresh_token") or data.get("refreshToken")
    assert refresh

    r = await api.post("/api/v1/admin/auth/refresh", json={"refresh_token": refresh})
    assert r.status_code == 200, r.text
    fresh = r.json()["data"]["token"]
    headers = {"Authorization": f"Bearer {fresh}"}

    r = await api.post("/api/v1/admin/auth/refresh", json={"refresh_token": fresh})
    assert r.status_code == 401, "an access token is not a refresh token"
    r = await api.post("/api/v1/admin/auth/refresh", json={"refresh_token": "garbage"})
    assert r.status_code == 401

    r = await api.get("/api/v1/admin/auth/profile", headers=headers)
    assert r.status_code == 200 and admin.email in r.text
    assert "password" not in r.text.lower() or "password_hash" not in r.text
    r = await api.put("/api/v1/admin/auth/profile", json={"full_name": "ZZ Renamed Manager"}, headers=headers)
    assert r.status_code == 200 and "ZZ Renamed Manager" in r.text

    r = await api.post("/api/v1/admin/auth/logout", headers=headers)
    assert r.status_code == 200, r.text


async def test_changing_an_admin_password(api, world, make_admin):
    admin = await make_admin("inventory_manager", api=api)
    new = _pw()
    r = await api.post("/api/v1/admin/auth/change-password",
                       json={"current_password": "not-it-at-all", "new_password": new}, headers=admin.headers)
    assert r.status_code == 400, r.text
    r = await api.post("/api/v1/admin/auth/change-password",
                       json={"current_password": admin.password, "new_password": "short"}, headers=admin.headers)
    assert r.status_code == 422
    r = await api.post("/api/v1/admin/auth/change-password",
                       json={"current_password": admin.password, "new_password": new}, headers=admin.headers)
    assert r.status_code == 200, r.text
    assert (await _login(api, admin.email, admin.password)).status_code == 401
    assert (await _login(api, admin.email, new)).status_code == 200


async def test_a_deactivated_admins_token_stops_working(api, world, make_admin):
    boss = await make_admin("super_admin", api=api)
    victim = await make_admin("marketing_manager", api=api)
    assert (await api.get("/api/v1/admin/auth/profile", headers=victim.headers)).status_code == 200
    r = await api.delete(f"/api/v1/admin/users/{victim.admin_id}", headers=boss.headers)
    assert r.status_code == 200, r.text
    r = await api.get("/api/v1/admin/auth/profile", headers=victim.headers)
    assert r.status_code == 401, "a live token dies with the account"


async def test_admin_login_does_not_reveal_which_emails_exist(api, world, make_admin):
    admin = await make_admin("customer_support", api=api)
    wrong = await _login(api, admin.email, "definitely-wrong-1")
    nobody = await _login(api, f"nobody.{uuid.uuid4().hex[:6]}@sribees-ci.lk", "definitely-wrong-1")
    assert wrong.status_code == nobody.status_code == 401
    assert wrong.json() == nobody.json()
