"""
Partner portal "forgot password": a code by email, then a new password.

Pins the brief's rules end to end against the real endpoints: the code works
once, five wrong guesses burn it, a resend waits 60 s, an unknown email is
answered exactly like a real one, and a reset signs the partner out of every
earlier session.
"""
from __future__ import annotations

import asyncio
import re
import secrets
import uuid

import pytest


def _pw() -> str:
    return f"Zz{secrets.token_urlsafe(10)}9!"


def _ip() -> dict:
    # The auth limiter is per address; each call gets its own.
    return {"X-Forwarded-For": f"198.51.100.{uuid.uuid4().int % 250 + 1}"}


class _Outbox(list):
    async def settle(self):
        """The mail goes out after the response; wait for it."""
        from app.services.partner_password_reset import _DELIVERIES

        if _DELIVERIES:
            await asyncio.gather(*list(_DELIVERIES))


@pytest.fixture
def outbox(monkeypatch):
    """Every reset mail, instead of SMTP."""
    from app.services import email_sender, partner_password_reset

    sent = _Outbox()

    async def _send(to, subject, text, html):
        sent.append({"to": to, "subject": subject, "text": text, "html": html})

    monkeypatch.setattr(email_sender, "is_configured", lambda: True)
    monkeypatch.setattr(partner_password_reset, "send_email", _send)
    return sent


@pytest.fixture
async def partner(api, make_admin):
    boss = await make_admin("super_admin", api=api)
    email = f"zz.reset.{uuid.uuid4().hex[:8]}@sribees-ci.lk"
    password = _pw()
    r = await api.post("/api/v1/admin/partners",
                       json={"email": email, "password": password, "full_name": "ZZ Reset"},
                       headers=boss.headers)
    assert r.status_code == 201, r.text
    return {"email": email, "password": password}


def _code_from(mail: dict) -> str:
    return re.search(r"\b(\d{6})\b", mail["text"]).group(1)


async def _forgot(api, email, outbox=None):
    r = await api.post("/api/v1/partner/auth/forgot-password", json={"email": email}, headers=_ip())
    if outbox is not None:
        await outbox.settle()
    return r


async def _verify(api, email, otp):
    return await api.post("/api/v1/partner/auth/verify-otp", json={"email": email, "otp": otp}, headers=_ip())


async def _reset(api, email, password, **proof):
    return await api.post("/api/v1/partner/auth/reset-password",
                          json={"email": email, "new_password": password, "confirm_password": password, **proof},
                          headers=_ip())


async def _login(api, email, password):
    return await api.post("/api/v1/partner/auth/login", json={"email": email, "password": password},
                          headers=_ip())


def _wrong(code: str) -> str:
    return f"{(int(code) + 1) % 1_000_000:06d}"


async def test_the_whole_reset_signs_out_old_sessions(api, partner, outbox):
    email = partner["email"]
    old = (await _login(api, email, partner["password"])).json()["data"]["token"]
    old_headers = {"Authorization": f"Bearer {old}"}
    assert (await api.get("/api/v1/partner/me", headers=old_headers)).status_code == 200

    r = await _forgot(api, email.upper(), outbox)
    assert r.status_code == 200, r.text
    assert r.json() == {
        "success": True,
        "message": "Verification code has been sent to your email address.",
        "data": {"email": email, "expires_in": 300},
    }
    assert len(outbox) == 1 and outbox[0]["to"] == email
    code = _code_from(outbox[0])
    assert code in outbox[0]["subject"] and "SRIBEES" in outbox[0]["html"]

    r = await _verify(api, email, _wrong(code))
    assert r.status_code == 400 and r.json()["message"] == "Invalid or expired OTP code."

    r = await _verify(api, email, code)
    assert r.status_code == 200, r.text
    token = r.json()["data"]["reset_token"]

    new = _pw()
    r = await _reset(api, email, new, otp=code, reset_token=token)
    assert r.status_code == 200, r.text
    assert r.json()["message"].startswith("Password has been reset successfully")

    # One use only, and every earlier session is gone.
    assert (await _reset(api, email, _pw(), reset_token=token)).status_code == 400
    assert (await _reset(api, email, _pw(), otp=code)).status_code == 400
    assert (await api.get("/api/v1/partner/me", headers=old_headers)).status_code == 401
    assert (await _login(api, email, partner["password"])).status_code == 401
    r = await _login(api, email, new)
    assert r.status_code == 200, r.text
    fresh = {"Authorization": f"Bearer {r.json()['data']['token']}"}
    assert (await api.get("/api/v1/partner/me", headers=fresh)).status_code == 200


async def test_reset_with_the_code_alone(api, partner, outbox):
    """The portal brief sends the OTP to reset-password; that works too."""
    await _forgot(api, partner["email"], outbox)
    code = _code_from(outbox[-1])
    assert (await _verify(api, partner["email"], code)).status_code == 200
    new = _pw()
    assert (await _reset(api, partner["email"], new, otp=code)).status_code == 200
    assert (await _login(api, partner["email"], new)).status_code == 200


async def test_an_unknown_email_gets_the_same_answer_and_no_mail(api, partner, outbox):
    known = await _forgot(api, partner["email"], outbox)
    unknown = await _forgot(api, f"zz.nobody.{uuid.uuid4().hex[:8]}@sribees-ci.lk", outbox)
    assert unknown.status_code == known.status_code == 200
    assert unknown.json()["message"] == known.json()["message"]
    assert [m["to"] for m in outbox] == [partner["email"]]

    # Wrong guesses read the same too, up to and including the lock-out.
    nobody = f"zz.nobody.{uuid.uuid4().hex[:8]}@sribees-ci.lk"
    await _forgot(api, nobody, outbox)
    answers = [(await _verify(api, nobody, "123456")).status_code for _ in range(6)]
    assert answers == [400, 400, 400, 400, 429, 429]


async def test_guesses_sent_together_still_get_five_tries(api, partner, outbox):
    await _forgot(api, partner["email"], outbox)
    code = _code_from(outbox[-1])
    answers = await asyncio.gather(*[_verify(api, partner["email"], _wrong(code)) for _ in range(12)])
    assert sum(r.status_code == 400 for r in answers) == 4
    assert (await _verify(api, partner["email"], code)).status_code == 429


async def test_a_failed_send_lets_the_partner_ask_again(api, partner, monkeypatch):
    from app.services import email_sender, partner_password_reset
    from app.services.email_sender import EmailSendError

    async def _down(*_):
        raise EmailSendError("SMTP down")

    monkeypatch.setattr(email_sender, "is_configured", lambda: True)
    monkeypatch.setattr(partner_password_reset, "send_email", _down)
    assert (await _forgot(api, partner["email"])).status_code == 200
    await _Outbox().settle()
    assert (await _forgot(api, partner["email"])).status_code == 200


async def test_the_name_cannot_put_markup_in_the_mail():
    from app.services.partner_password_reset import _email_bodies

    _, _, body = _email_bodies('<a href="https://evil">x</a>', "123456")
    assert "<a href=\"https://evil\">" not in body and "&lt;a href=" in body


async def test_a_resend_waits_a_minute_and_replaces_the_code(api, partner, outbox, app_redis):
    from app.services.partner_password_reset import _key

    assert (await _forgot(api, partner["email"], outbox)).status_code == 200
    r = await _forgot(api, partner["email"])
    assert r.status_code == 429
    assert r.json()["message"] == "Please wait before requesting another verification code."
    assert r.json()["error"]["retry_after"] > 0

    first = _code_from(outbox[-1])
    await app_redis.delete(_key("wait", partner["email"]))  # the minute passes
    assert (await _forgot(api, partner["email"], outbox)).status_code == 200
    second = _code_from(outbox[-1])
    if first != second:
        assert (await _verify(api, partner["email"], first)).status_code == 400
    assert (await _verify(api, partner["email"], second)).status_code == 200


async def test_five_wrong_guesses_burn_the_code(api, partner, outbox):
    await _forgot(api, partner["email"], outbox)
    code = _code_from(outbox[-1])
    for _ in range(4):
        assert (await _verify(api, partner["email"], _wrong(code))).status_code == 400
    r = await _verify(api, partner["email"], _wrong(code))
    assert r.status_code == 429
    assert r.json()["message"] == "Too many failed attempts. Please request a new OTP."
    # The right code is no use now.
    assert (await _verify(api, partner["email"], code)).status_code == 429
    assert (await _reset(api, partner["email"], _pw(), otp=code)).status_code in (400, 429)


async def test_bad_input_is_a_400(api, partner, outbox):
    r = await _forgot(api, "not-an-email")
    assert r.status_code == 400 and r.json()["message"] == "Invalid email address format."

    await _forgot(api, partner["email"], outbox)
    code = _code_from(outbox[-1])
    for new, confirm in [("short1!", "short1!"), (_pw(), _pw()), ("x" * 129, "x" * 129)]:
        r = await api.post("/api/v1/partner/auth/reset-password",
                           json={"email": partner["email"], "otp": code,
                                 "new_password": new, "confirm_password": confirm},
                           headers=_ip())
        assert r.status_code == 400 and r.json()["error"]["code"] == "INVALID_PASSWORD"
    # Neither attempt used the code up.
    assert (await _reset(api, partner["email"], _pw(), otp=code)).status_code == 200


async def test_without_smtp_the_server_says_so(api, partner, monkeypatch):
    from app.services import email_sender

    monkeypatch.setattr(email_sender, "is_configured", lambda: False)
    r = await _forgot(api, partner["email"])
    assert r.status_code == 503
    assert r.json()["error"]["code"] == "EMAIL_UNAVAILABLE"
