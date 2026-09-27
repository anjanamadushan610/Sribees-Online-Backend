"""
OTP Service — short-lived phone-verification codes (Module 1.7).

Codes are 6 digits, stored in Redis under a per-user key with a 3-minute TTL,
and delivered through app/services/sms_gateway.py -- whichever provider this
server is configured for.

Endpoints call ``OTPService.issue``: it applies the sending limits, sends, and
returns an ``OtpChallenge`` that tells the app everything it draws -- how many
digits, how long until "Resend" unlocks, where the code went. The app decides
none of it, so a change of provider or policy never needs an app release.
"""
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from fastapi import status
from loguru import logger
from redis.asyncio import Redis

from app.config.redis import RedisTTL
from app.config.settings import settings
from app.core.exceptions import AppException
from app.services.sms_gateway import SmsSendError, get_sms_gateway, mask_phone

_OTP_DIGITS = 6
_LK_TZ = ZoneInfo("Asia/Colombo")

# What the customer reads, in the app's language (Accept-Language). The SMS
# itself stays English: a Sinhala or Tamil text is billed as two or three.
_COPY = {
    "sent": {
        "en": "We sent a {length}-digit code to {destination}.",
        "si": "{destination} අංකයට ඉලක්කම් {length} කේතයක් යැව්වා.",
        "ta": "{destination} எண்ணுக்கு {length} இலக்கக் குறியீடு அனுப்பப்பட்டது.",
    },
    "too_soon": {
        "en": "Please wait {seconds} seconds before asking for another code.",
        "si": "තවත් කේතයක් ඉල්ලීමට තත්පර {seconds}ක් රැඳී සිටින්න.",
        "ta": "இன்னொரு குறியீட்டைக் கேட்க {seconds} வினாடிகள் காத்திருக்கவும்.",
    },
    "too_many": {
        "en": "Too many codes were asked for this number. Please try again in {minutes} minutes.",
        "si": "මෙම අංකයට කේත බොහොමයක් ඉල්ලා ඇත. මිනිත්තු {minutes}කින් නැවත උත්සාහ කරන්න.",
        "ta": "இந்த எண்ணுக்கு அதிகமான குறியீடுகள் கோரப்பட்டுள்ளன. {minutes} நிமிடங்களில் மீண்டும் முயற்சிக்கவும்.",
    },
    "unavailable": {
        "en": "We couldn't send a code right now. Please try again in a few minutes.",
        "si": "දැන් කේතය යැවීමට නොහැකි විය. මිනිත්තු කිහිපයකින් නැවත උත්සාහ කරන්න.",
        "ta": "இப்போது குறியீட்டை அனுப்ப முடியவில்லை. சில நிமிடங்களில் மீண்டும் முயற்சிக்கவும்.",
    },
    "invalid": {
        "en": "That code is not right, or it has expired.",
        "si": "එම කේතය වැරදියි, නැතහොත් කල් ඉකුත් වී ඇත.",
        "ta": "அந்தக் குறியீடு தவறானது அல்லது காலாவதியானது.",
    },
}

_SMS_TEXT = (
    "{code} is your Sribees Online verification code. "
    "It expires in {minutes} minutes. Never share it with anyone."
)


def otp_copy(key: str, lang: str, **values) -> str:
    table = _COPY[key]
    return table.get(lang, table["en"]).format(**values)


def invalid_code_error(lang: str) -> AppException:
    """The one answer for a wrong, used, expired or burned code."""
    return AppException(
        status_code=status.HTTP_400_BAD_REQUEST,
        message=otp_copy("invalid", lang),
        error_code="OTP_INVALID",
    )


@dataclass(frozen=True)
class OtpChallenge:
    """What the app needs to draw the code screen. Nothing else is assumed."""

    expires_in_seconds: int
    resend_in_seconds: int
    code_length: int
    destination: str
    message: str

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

    # Sending rules, the same on every server (the owner's, 2026-09-27). The
    # daily total for the whole server is not here: the Super Admin sets it.
    RESEND_COOLDOWN_SECONDS = 5 * 60
    MAX_SENDS_PER_PHONE_PER_HOUR = 5
    MAX_SENDS_PER_PHONE_PER_DAY = 10
    MAX_SENDS_PER_IP_PER_HOUR = 10
    # How long each day's count is kept, for the SMS page's history.
    HISTORY_DAYS = 8

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
        send it. Overwrites any existing code under that key. No limits here:
        endpoints go through ``issue``. Raises SmsSendError when the provider
        refuses, after removing the code.

        ``key`` is caller-supplied so the same flow serves both an
        already-authenticated user re-verifying their phone
        (``RedisKeys.phone_otp(user_id)``) and phone-first registration,
        where there is no account yet (``RedisKeys.phone_registration_otp(phone)``).
        """
        code = OTPService._generate_code()
        await redis.set(key, code, ex=RedisTTL.PHONE_OTP)
        if is_test_phone(phone):
            # Listed test numbers sign in with the test code; most are not
            # real phones, and a text to them is credit thrown away.
            logger.info(f"OTP for listed test number {phone} not texted")
            return
        text = _SMS_TEXT.format(code=code, minutes=RedisTTL.PHONE_OTP // 60)
        try:
            await get_sms_gateway().send(phone, text)
        except SmsSendError:
            # A code nobody received must not linger as a guessable target.
            await redis.delete(key)
            raise

    # ------------------------------------------------------------------
    # Sending limits
    # ------------------------------------------------------------------

    @staticmethod
    def _lk_day() -> str:
        return datetime.now(timezone.utc).astimezone(_LK_TZ).strftime("%Y%m%d")

    @staticmethod
    async def _count(redis: Redis, key: str, window: int) -> tuple[int, int]:
        """Add one to a windowed counter. Returns (count, seconds left)."""
        count = await redis.incr(key)
        if count == 1:
            await redis.expire(key, window)
        ttl = await redis.ttl(key)
        return count, ttl if ttl and ttl > 0 else window

    @staticmethod
    def _refuse(code: str, message: str, retry_after: int, http: int = status.HTTP_429_TOO_MANY_REQUESTS):
        return AppException(
            status_code=http,
            message=message,
            error_code=code,
            details={"retry_after": max(1, int(retry_after))},
        )

    @staticmethod
    async def issue(
        redis: Redis,
        key: str,
        phone: str,
        *,
        client_ip: Optional[str] = None,
        lang: str = "en",
        daily_limit: int,
    ) -> OtpChallenge:
        """
        Send a code to ``phone`` if the sending limits allow it.

        The request endpoint needs no login and every text is paid for, so
        the limits are about money as much as about the recipient: a resend
        cooldown and hourly/daily caps per number (nobody gets text-bombed),
        a per-address cap, and a daily ceiling for the whole server
        (``daily_limit``, the Super Admin's setting; 0 sends nothing) that no
        amount of rotating numbers and addresses gets past.
        """
        from app.utils.identity import normalize_mobile

        phone = normalize_mobile(phone)
        destination = mask_phone(phone)
        cooldown = max(0, OTPService.RESEND_COOLDOWN_SECONDS)
        challenge = OtpChallenge(
            expires_in_seconds=RedisTTL.PHONE_OTP,
            resend_in_seconds=cooldown,
            code_length=_OTP_DIGITS,
            destination=destination,
            message=otp_copy("sent", lang, length=_OTP_DIGITS, destination=destination),
        )
        if is_test_phone(phone):
            await OTPService.request_otp(redis, key, phone)
            return challenge

        cooldown_key = f"otp:send:wait:{phone}"
        if cooldown > 0 and not await redis.set(cooldown_key, "1", ex=cooldown, nx=True):
            wait = await redis.ttl(cooldown_key)
            wait = wait if wait and wait > 0 else cooldown
            raise OTPService._refuse("OTP_RESEND_TOO_SOON", otp_copy("too_soon", lang, seconds=wait), wait)

        day = OTPService._lk_day()
        limits = [
            (f"otp:send:hour:{phone}", 3600, OTPService.MAX_SENDS_PER_PHONE_PER_HOUR),
            (f"otp:send:day:{phone}:{day}", 86400, OTPService.MAX_SENDS_PER_PHONE_PER_DAY),
        ]
        if client_ip:
            limits.append((f"otp:send:ip:{client_ip}", 3600, OTPService.MAX_SENDS_PER_IP_PER_HOUR))
        for counter, window, limit in limits:
            count, left = await OTPService._count(redis, counter, window)
            if limit > 0 and count > limit:
                logger.warning(f"OTP send limit hit on {counter.split(':')[2]} ({count}/{limit})")
                raise OTPService._refuse(
                    "OTP_TOO_MANY_REQUESTS",
                    otp_copy("too_many", lang, minutes=max(1, (left + 59) // 60)),
                    left,
                )

        budget_key = f"sms:sent:{day}"
        sent_today, _ = await OTPService._count(redis, budget_key, OTPService.HISTORY_DAYS * 86400)
        if sent_today > daily_limit:
            await redis.decr(budget_key)
            await redis.delete(cooldown_key)
            logger.error(
                f"SMS daily limit reached ({daily_limit}); refusing codes until tomorrow. "
                "The Super Admin can raise it on the SMS page."
            )
            raise OTPService._refuse(
                "OTP_SMS_UNAVAILABLE",
                otp_copy("unavailable", lang),
                600,
                http=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        try:
            await OTPService.request_otp(redis, key, phone)
        except SmsSendError as exc:
            # Nothing was delivered: give back the credit and the cooldown so
            # the customer can simply try again.
            await redis.decr(budget_key)
            await redis.delete(cooldown_key)
            logger.error(f"SMS to {destination} failed: {exc}")
            raise OTPService._refuse(
                "OTP_SMS_UNAVAILABLE",
                otp_copy("unavailable", lang),
                30,
                http=status.HTTP_503_SERVICE_UNAVAILABLE,
            ) from exc
        return challenge

    @staticmethod
    async def sent_today(redis: Redis) -> int:
        value = await redis.get(f"sms:sent:{OTPService._lk_day()}")
        return int(value) if value else 0

    @staticmethod
    async def sent_by_day(redis: Redis, days: int = 7) -> list[dict]:
        """Texts sent on each of the last ``days`` Sri Lankan days, newest first."""
        from datetime import timedelta

        today = datetime.now(timezone.utc).astimezone(_LK_TZ).date()
        out = []
        for back in range(days):
            day = today - timedelta(days=back)
            value = await redis.get(f"sms:sent:{day.strftime('%Y%m%d')}")
            out.append({"date": day.isoformat(), "sent": int(value) if value else 0})
        return out

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
