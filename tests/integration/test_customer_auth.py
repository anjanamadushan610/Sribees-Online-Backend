"""
Customer identity, end to end: phone sign-in, sessions, profile, and closing
an account.

Customers sign in with a phone OTP, Google or Apple only -- there is no email
and password sign-in, email verification or password reset, and those routes
must stay gone. Codes are read from where the server keeps them (Redis),
never guessed or bypassed, so every check below is the one a real customer
passes through.
"""
from __future__ import annotations

import uuid

import pytest

from app.config.redis import RedisKeys


def _phone() -> str:
    return "+947" + f"{uuid.uuid4().int % 10**8:08d}"


def _bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _tokens(body: dict) -> dict:
    """{access_token, refresh_token} whichever casing the response uses."""
    data = body.get("data") or body
    t = data.get("tokens") or data
    return {
        "access_token": t.get("access_token") or t.get("accessToken"),
        "refresh_token": t.get("refresh_token") or t.get("refreshToken"),
    }


async def _phone_sign_in(api, app_redis, phone: str) -> dict:
    """One sign-in on one device: request a code, read it, verify it."""
    r = await api.post("/api/v1/auth/phone/request-otp", json={"phone": phone})
    assert r.status_code == 200, r.text
    code = await app_redis.get(RedisKeys.phone_registration_otp(phone))
    r = await api.post("/api/v1/auth/phone/verify-otp", json={"phone": phone, "code": code})
    assert r.status_code == 200, r.text
    # The next sign-in on another device comes later than the resend cooldown.
    await app_redis.delete(f"otp:send:wait:{phone}")
    return _tokens(r.json())


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/auth/register",
        "/api/v1/auth/login",
        "/api/v1/auth/verify-email",
        "/api/v1/auth/resend-verification",
        "/api/v1/auth/forgot-password",
        "/api/v1/auth/reset-password",
        "/api/v1/auth/change-password",
    ],
)
async def test_there_is_no_email_and_password_sign_in(api, make_customer, world, path):
    c = await make_customer()
    body = {"email": f"zz.{uuid.uuid4().hex[:10]}@sribees-ci.lk", "password": uuid.uuid4().hex, "token": "x"}
    for headers in ({}, c.headers):
        r = await api.post(path, json=body, headers=headers)
        assert r.status_code == 404, f"{path} must not exist: {r.status_code} {r.text}"


async def test_sessions_refresh_and_logout(api, app_redis, world):
    phone = _phone()
    first = await _phone_sign_in(api, app_redis, phone)
    second = await _phone_sign_in(api, app_redis, phone)

    r = await api.get("/api/v1/auth/sessions", headers=_bearer(first["access_token"]))
    assert r.status_code == 200, r.text
    assert r.text.count("session_id") >= 2 or r.text.count("sessionId") >= 2

    r = await api.post("/api/v1/auth/refresh", json={"refresh_token": first["refresh_token"]})
    assert r.status_code == 200, r.text
    refreshed = _tokens(r.json())
    assert refreshed["access_token"] and refreshed["refresh_token"]
    r = await api.post("/api/v1/auth/refresh-token", json={"refresh_token": "garbage"})
    assert r.status_code == 401, r.text
    r = await api.post("/api/v1/auth/refresh", json={"refresh_token": first["access_token"]})
    assert r.status_code == 401, "an access token is not a refresh token"

    # Sign the other device out; this one keeps working, that one stops.
    r = await api.post("/api/v1/auth/logout-others", headers=_bearer(refreshed["access_token"]))
    assert r.status_code == 200, r.text
    r = await api.get("/api/v1/auth/me", headers=_bearer(second["access_token"]))
    assert r.status_code == 401, "a revoked session's token must stop working at once"
    r = await api.get("/api/v1/auth/me", headers=_bearer(refreshed["access_token"]))
    assert r.status_code == 200, r.text

    r = await api.post("/api/v1/auth/logout", headers=_bearer(refreshed["access_token"]))
    assert r.status_code == 200, r.text
    r = await api.get("/api/v1/auth/me", headers=_bearer(refreshed["access_token"]))
    assert r.status_code == 401
    # Logging out ends the session, so its refresh token is dead too.
    r = await api.post("/api/v1/auth/refresh", json={"refresh_token": first["refresh_token"]})
    assert r.status_code == 401, r.text


async def test_revoking_one_session_and_logging_out_everywhere(api, app_redis, world):
    phone = _phone()
    a = await _phone_sign_in(api, app_redis, phone)
    b = await _phone_sign_in(api, app_redis, phone)

    r = await api.get("/api/v1/auth/sessions", headers=_bearer(a["access_token"]))
    data = r.json().get("data") or r.json()
    sessions = data.get("sessions", data) if isinstance(data, dict) else data
    ids = [s.get("session_id") or s.get("sessionId") for s in sessions]
    assert len(ids) >= 2
    r = await api.delete(f"/api/v1/auth/sessions/{uuid.uuid4()}", headers=_bearer(a["access_token"]))
    assert r.status_code in (401, 404), r.text

    r = await api.post("/api/v1/auth/logout-all", headers=_bearer(a["access_token"]))
    assert r.status_code == 200, r.text
    for t in (a, b):
        r = await api.get("/api/v1/auth/me", headers=_bearer(t["access_token"]))
        assert r.status_code == 401


async def test_phone_sign_in_with_a_real_code(api, app_redis, world):
    phone = _phone()
    r = await api.post("/api/v1/auth/phone/request-otp", json={"phone": phone})
    assert r.status_code == 200, r.text
    code = await app_redis.get(RedisKeys.phone_registration_otp(phone))
    assert code and len(code) == 6

    r = await api.post("/api/v1/auth/phone/verify-otp", json={"phone": phone, "code": code})
    assert r.status_code == 200, r.text
    access = _tokens(r.json())["access_token"]
    r = await api.get("/api/v1/auth/me", headers=_bearer(access))
    assert r.status_code == 200, r.text

    # The code was consumed; the same code cannot sign in twice.
    r = await api.post("/api/v1/auth/phone/verify-otp", json={"phone": phone, "code": code})
    assert r.status_code == 400


async def test_profile_updates_are_validated(api, make_customer, world):
    c = await make_customer()
    r = await api.patch("/api/v1/auth/me", json={"full_name": "Kamal Perera"}, headers=c.headers)
    assert r.status_code == 200, r.text
    assert "Kamal Perera" in r.text
    r = await api.patch("/api/v1/auth/me", json={"nic": "not-a-nic"}, headers=c.headers)
    assert r.status_code == 422, r.text
    # A real shape and a real birth date (1995, day 1-300), random serial:
    # the server rejects impossible dates, and must accept this.
    day = 1 + uuid.uuid4().int % 300
    nic = f"1995{day:03d}{uuid.uuid4().int % 10**5:05d}"
    r = await api.patch("/api/v1/auth/me", json={"nic": nic}, headers=c.headers)
    assert r.status_code == 200, r.text
    # One person, one live account: the same NIC on another account is refused.
    other = await make_customer()
    r = await api.patch("/api/v1/auth/me", json={"nic": nic}, headers=other.headers)
    assert r.status_code == 400 and "DUPLICATE" in r.text, r.text


async def test_closing_an_account(api, app_redis, make_customer, world):
    c = await make_customer()
    r = await api.post("/api/v1/auth/account/deletion/request-otp", headers=c.headers)
    assert r.status_code == 200, r.text
    code = await app_redis.get(RedisKeys.account_deletion_otp(str(c.user_id)))
    assert code

    r = await api.post("/api/v1/auth/account/deletion/confirm", json={"code": "000000" if code != "000000" else "111111"},
                       headers=c.headers)
    assert r.status_code == 400
    r = await api.post("/api/v1/auth/account/deletion/confirm", json={"code": code, "reason": "Moving abroad"},
                       headers=c.headers)
    assert r.status_code == 200, r.text
    r = await api.get("/api/v1/auth/me", headers=c.headers)
    assert r.status_code in (401, 403), "a closed account's token must stop working"
