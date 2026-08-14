"""
OTP Service — short-lived phone-verification codes (Module 1.7).

Codes are 6 digits, stored in Redis under a per-user key with a 3-minute TTL.
For the MVP the "SMS gateway" is a console log — swap ``_dispatch`` for a real
provider (Notify.lk / Twilio) later without touching the endpoints.
"""
import secrets

from loguru import logger
from redis.asyncio import Redis

from app.config.redis import RedisTTL
from app.config.settings import settings

_OTP_DIGITS = 6


class OTPService:
    """Generate, store, dispatch and verify phone OTP codes."""

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
    async def verify_otp(redis: Redis, key: str, code: str) -> bool:
        """
        Validate ``code`` against the OTP stored under ``key``. On success the
        code is consumed (deleted) so it can't be reused. Returns False if
        missing, expired or mismatched.

        TEMPORARY: if settings.otp_test_bypass_code is set and matches, this
        short-circuits to True without touching Redis at all -- see the
        setting's docstring for removal instructions.
        """
        submitted = code.strip()
        if settings.otp_test_bypass_code and submitted == settings.otp_test_bypass_code:
            return True

        stored = await redis.get(key)
        if stored is None:
            return False

        # Redis may return bytes or str depending on decode_responses.
        stored_str = stored.decode() if isinstance(stored, (bytes, bytearray)) else str(stored)
        if not secrets.compare_digest(stored_str, submitted):
            return False

        await redis.delete(key)
        return True
