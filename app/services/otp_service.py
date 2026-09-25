"""
OTP Service — short-lived phone-verification codes (Module 1.7).

Codes are 6 digits, stored in Redis under a per-user key with a 3-minute TTL.
For the MVP the "SMS gateway" is a console log — swap ``_dispatch`` for a real
provider (Notify.lk / Twilio) later without touching the endpoints.
"""
import secrets
from typing import Optional

from loguru import logger
from redis.asyncio import Redis

from app.config.redis import RedisTTL
from app.config.settings import settings

_OTP_DIGITS = 6

# The test OTP. It verifies ONLY for the numbers in settings.otp_test_phones
# (testers, store-review demo accounts), never for anyone else. It used to
# verify for every phone number in production, so anyone could sign in as any
# phone-registered customer (SEC-001). Delete it once real SMS delivery is live
# and the reviewers have a real test number.
_TEST_BYPASS_CODE = "123456"


def _test_phones() -> frozenset[str]:
    from app.utils.identity import normalize_mobile

    phones = set()
    for raw in (settings.otp_test_phones or "").split(","):
        if raw.strip():
            try:
                phones.add(normalize_mobile(raw))
            except ValueError:
                logger.error(f"OTP_TEST_PHONES has an entry that is not a mobile number: {raw!r}")
    return frozenset(phones)


def is_test_phone(phone: Optional[str]) -> bool:
    """Whether ``phone`` may use the test OTP."""
    from app.utils.identity import normalize_mobile

    if not phone:
        return False
    try:
        return normalize_mobile(phone) in _test_phones()
    except ValueError:
        return False


class OTPService:
    """Generate, store, dispatch and verify phone OTP codes."""

    # Wrong guesses allowed against one key before its code is thrown away.
    # A 6-digit code is a million possibilities; without a cap, the only thing
    # standing between a phone number and its account was the per-IP request
    # limit, which rotating addresses defeats. The counter deliberately
    # survives a resend, so asking for a fresh code does not buy fresh guesses.
    MAX_ATTEMPTS = 5

    @staticmethod
    def _attempts_key(key: str) -> str:
        return f"{key}:attempts"

    @staticmethod
    def _generate_code() -> str:
        """A cryptographically-random zero-padded 6-digit code."""
        return f"{secrets.randbelow(10 ** _OTP_DIGITS):0{_OTP_DIGITS}d}"

    @staticmethod
    async def request_otp(redis: Redis, key: str, phone: str) -> None:
        """
        Generate an OTP, store it in Redis under ``key`` (3-min expiry) and
        'send' it. Overwrites any existing code under that key.

        ``key`` is caller-supplied so the same flow serves both an
        already-authenticated user re-verifying their phone
        (``RedisKeys.phone_otp(user_id)``) and phone-first registration,
        where there is no account yet (``RedisKeys.phone_registration_otp(phone)``).
        """
        code = OTPService._generate_code()
        await redis.set(key, code, ex=RedisTTL.PHONE_OTP)
        OTPService._dispatch(phone, code)

    @staticmethod
    def _dispatch(phone: str, code: str) -> None:
        """Mock SMS gateway: log the code to the console (MVP)."""
        target = phone or "<no phone on file>"
        logger.info(
            f"[MOCK SMS] SRIBEESonline verification code for {target}: {code} "
            f"(valid {RedisTTL.PHONE_OTP // 60} min)"
        )

    @staticmethod
    async def verify_otp(redis: Redis, key: str, code: str, *, phone: Optional[str] = None) -> bool:
        """
        Validate ``code`` against the OTP stored under ``key``. On success the
        code is consumed (deleted) so it can't be reused. Returns False if
        missing, expired or mismatched.

        ``phone`` is the number the code was sent to. The test OTP verifies
        only when it is on the OTP_TEST_PHONES list; see ``_TEST_BYPASS_CODE``.
        """
        submitted = code.strip()
        if submitted == _TEST_BYPASS_CODE and is_test_phone(phone):
            logger.info(f"Test OTP accepted for listed test number {phone}")
            return True

        stored = await redis.get(key)
        if stored is None:
            return False

        # Redis may return bytes or str depending on decode_responses.
        stored_str = stored.decode() if isinstance(stored, (bytes, bytearray)) else str(stored)
        if not secrets.compare_digest(stored_str, submitted):
            attempts_key = OTPService._attempts_key(key)
            attempts = await redis.incr(attempts_key)
            if attempts == 1:
                await redis.expire(attempts_key, RedisTTL.PHONE_OTP)
            if attempts >= OTPService.MAX_ATTEMPTS:
                # Burn the code: the next guess has nothing to match.
                await redis.delete(key)
                logger.warning(f"OTP locked after {attempts} wrong attempts: {key}")
            return False

        await redis.delete(key, OTPService._attempts_key(key))
        return True
