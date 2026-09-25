"""
One-time codes: phone sign-in, phone re-verification and account closure.

A 6-digit code is a million possibilities and phone numbers are public, so the
only real protection is how many guesses one code tolerates. These tests pin
that, plus single use and expiry.
"""
from __future__ import annotations

import fakeredis.aioredis
import pytest

from app.services.otp_service import _TEST_BYPASS_CODE, OTPService

pytestmark = pytest.mark.security

KEY = "otp:phone_reg:+94770000001"


@pytest.fixture
async def redis():
    r = fakeredis.aioredis.FakeRedis(decode_responses=True)
    yield r
    await r.aclose()


def _wrong(code: str) -> str:
    """A code that is neither right nor the temporary bypass."""
    for candidate in ("000000", "111111", "222222"):
        if candidate not in {code, _TEST_BYPASS_CODE}:
            return candidate
    raise AssertionError("unreachable")


async def test_a_code_verifies_once_and_is_then_consumed(redis):
    await OTPService.request_otp(redis, KEY, "+94770000001")
    code = await redis.get(KEY)
    assert code and len(code) == 6 and code.isdigit()
    assert await OTPService.verify_otp(redis, KEY, code) is True
    assert await OTPService.verify_otp(redis, KEY, code) is False, "a code must be single-use"


async def test_codes_expire(redis):
    await OTPService.request_otp(redis, KEY, "+94770000001")
    ttl = await redis.ttl(KEY)
    assert 0 < ttl <= 3 * 60


async def test_codes_are_not_predictable(redis):
    seen = set()
    for _ in range(40):
        await OTPService.request_otp(redis, KEY, "+94770000001")
        seen.add(await redis.get(KEY))
    assert len(seen) > 35


async def test_a_code_is_burned_after_max_wrong_guesses(redis):
    await OTPService.request_otp(redis, KEY, "+94770000001")
    code = await redis.get(KEY)
    for _ in range(OTPService.MAX_ATTEMPTS):
        assert await OTPService.verify_otp(redis, KEY, _wrong(code)) is False
    assert await OTPService.verify_otp(redis, KEY, code) is False, (
        "after MAX_ATTEMPTS wrong guesses even the right code must fail"
    )


async def test_a_resend_does_not_buy_fresh_guesses(redis):
    await OTPService.request_otp(redis, KEY, "+94770000001")
    code = await redis.get(KEY)
    for _ in range(OTPService.MAX_ATTEMPTS - 1):
        await OTPService.verify_otp(redis, KEY, _wrong(code))
    await OTPService.request_otp(redis, KEY, "+94770000001")
    fresh = await redis.get(KEY)
    assert await OTPService.verify_otp(redis, KEY, _wrong(fresh)) is False
    assert await redis.get(KEY) is None, "the next wrong guess after a resend burns the new code"


async def test_the_owner_can_still_sign_in_with_a_fresh_code_after_a_lockout(redis):
    await OTPService.request_otp(redis, KEY, "+94770000001")
    code = await redis.get(KEY)
    for _ in range(OTPService.MAX_ATTEMPTS):
        await OTPService.verify_otp(redis, KEY, _wrong(code))
    await OTPService.request_otp(redis, KEY, "+94770000001")
    assert await OTPService.verify_otp(redis, KEY, await redis.get(KEY)) is True


async def test_success_clears_the_attempt_counter(redis):
    await OTPService.request_otp(redis, KEY, "+94770000001")
    code = await redis.get(KEY)
    await OTPService.verify_otp(redis, KEY, _wrong(code))
    assert await OTPService.verify_otp(redis, KEY, code) is True
    assert await redis.get(f"{KEY}:attempts") is None


async def test_there_is_no_universal_code(redis, monkeypatch):
    """SEC-001: the test OTP works for NOBODY unless the number is listed."""
    from app.config.settings import settings

    monkeypatch.setattr(settings, "otp_test_phones", "")
    await OTPService.request_otp(redis, KEY, "+94770000001")
    assert await OTPService.verify_otp(redis, KEY, _TEST_BYPASS_CODE, phone="+94770000001") is False
    assert await OTPService.verify_otp(redis, KEY, _TEST_BYPASS_CODE) is False


async def test_the_test_code_works_only_for_listed_numbers(redis, monkeypatch):
    from app.config.settings import settings

    # Listed in two different shapes people type; both must match.
    monkeypatch.setattr(settings, "otp_test_phones", "0771112233, +94 77 444 5566, not-a-number")
    key = "otp:phone_reg:+94771112233"
    assert await OTPService.verify_otp(redis, key, _TEST_BYPASS_CODE, phone="+94771112233") is True
    assert await OTPService.verify_otp(redis, key, _TEST_BYPASS_CODE, phone="771112233") is True
    assert await OTPService.verify_otp(redis, key, _TEST_BYPASS_CODE, phone="0774445566") is True
    # A customer's number that is not on the list gets nothing from it.
    assert await OTPService.verify_otp(redis, KEY, _TEST_BYPASS_CODE, phone="+94770000001") is False
    assert await OTPService.verify_otp(redis, KEY, _TEST_BYPASS_CODE, phone=None) is False


async def test_phone_sign_in_refuses_the_test_code_for_an_unlisted_number(client, monkeypatch):
    """Over HTTP, through the real endpoint an attacker would use."""
    from app.config.settings import settings

    monkeypatch.setattr(settings, "otp_test_phones", "0771112233")
    r = await client.post(
        "/api/v1/auth/phone/verify-otp",
        json={"phone": "0770000001", "code": _TEST_BYPASS_CODE},
    )
    assert r.status_code == 400, r.text
