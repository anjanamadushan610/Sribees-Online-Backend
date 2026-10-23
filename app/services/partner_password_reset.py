"""
Partner password reset — a 6-digit code by email, then a new password.

    POST /partner/auth/forgot-password   email          -> code emailed
    POST /partner/auth/verify-otp        email + code   -> reset_token
    POST /partner/auth/reset-password    email + reset_token (or the code)
                                         + new password -> every old token dies

Everything lives in Redis, keyed by a hash of the email, and only hashes are
stored: a leaked Redis dump does not hand anyone a working code or address.

Rules (the partner portal brief, 2026-10-22):
  * the code is 6 cryptographically random digits and lives 5 minutes;
  * a new code needs 60 seconds since the last one, and replaces it;
  * 5 wrong guesses burn the code (the counter is shared by verify and reset,
    so the two endpoints together still allow only 5);
  * a successful reset deletes the code and the reset token, and stamps
    ``partners.password_changed_at`` so every token issued before it is
    refused by ``get_current_partner``.

**No account enumeration.** forgot-password answers an unknown or deactivated
email exactly as it answers a real one — same message, same cooldown, same
wrong-guess limit, and the mail itself goes out after the response — so the endpoint cannot be used to
find out who the partners are. Verify and reset answer "invalid or expired"
for an unknown email, as they do for a wrong code.
"""
import asyncio
import hashlib
import html
import json
import secrets
from datetime import datetime, timezone
from typing import Optional

from fastapi import status
from loguru import logger
from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import AppException
from app.core.security import hash_password
from app.models.partner import Partner
from app.services.email_sender import EmailSendError, mask_email, send_email

CODE_DIGITS = 6
CODE_TTL_SECONDS = 5 * 60
RESEND_COOLDOWN_SECONDS = 60
MAX_ATTEMPTS = 5
# How long the partner has to type a new password once the code is verified.
RESET_TOKEN_TTL_SECONDS = 10 * 60
MAX_SENDS_PER_EMAIL_PER_HOUR = 5
MAX_SENDS_PER_IP_PER_HOUR = 10
PASSWORD_MIN_LENGTH = 8
PASSWORD_MAX_LENGTH = 128

SUPPORT_EMAIL = "support@sribeesonline.com"
BRAND_COLOR = "#0F766E"  # the partner portal's primary colour


def reset_error(http: int, message: str, code: str, **extra) -> AppException:
    """The house error shape, plus a top-level ``message`` for the portal."""
    exc = AppException(status_code=http, message=message, error_code=code, details=extra or None)
    exc.detail["message"] = message
    return exc


def invalid_code() -> AppException:
    return reset_error(status.HTTP_400_BAD_REQUEST, "Invalid or expired OTP code.", "OTP_INVALID")


def too_many_attempts() -> AppException:
    return reset_error(
        status.HTTP_429_TOO_MANY_REQUESTS,
        "Too many failed attempts. Please request a new OTP.",
        "OTP_TOO_MANY_ATTEMPTS",
    )


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _normalise(email: str) -> str:
    return email.strip().lower()


# Keyed by the email, not the partner: an address that is not a partner runs
# through exactly the same counters, so no answer differs between the two.
def _key(kind: str, email: str) -> str:
    return f"partner:pwreset:{kind}:{_hash(_normalise(email))}"


async def _find_active(db: AsyncSession, email: str) -> Optional[Partner]:
    partner = (
        await db.execute(select(Partner).where(func.lower(Partner.email) == _normalise(email)))
    ).scalar_one_or_none()
    return partner if partner is not None and partner.is_active else None


async def _count(redis: Redis, key: str, window: int) -> tuple[int, int]:
    """Add one to a windowed counter. The window is set before the first
    INCR, so a counter can never be left without an expiry."""
    await redis.set(key, 0, ex=window, nx=True)
    count = await redis.incr(key)
    ttl = await redis.ttl(key)
    return count, ttl if ttl and ttl > 0 else window


def _email_bodies(name: str, code: str) -> tuple[str, str, str]:
    safe_name = html.escape(name)
    minutes = CODE_TTL_SECONDS // 60
    subject = f"{code} is your Sribees Partner password reset code"
    text = (
        f"Hello {name},\n\n"
        f"Use this code to reset your Sribees Partner Portal password:\n\n"
        f"    {code}\n\n"
        f"It expires in {minutes} minutes. Never share it with anyone - "
        f"Sribees staff will never ask for it.\n\n"
        f"If you did not ask to reset your password, ignore this email; "
        f"your password stays the same.\n\n"
        f"Sribees Online - {SUPPORT_EMAIL}\n"
    )
    spaced = " ".join(code)
    body = f"""<!doctype html>
<html><body style="margin:0;padding:0;background:#f3f4f6;font-family:Arial,Helvetica,sans-serif;color:#111827">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f3f4f6;padding:24px 12px">
<tr><td align="center">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:480px;background:#ffffff;border-radius:12px;overflow:hidden">
<tr><td style="background:{BRAND_COLOR};padding:20px 24px;color:#ffffff">
<div style="font-size:22px;font-weight:bold;letter-spacing:1px">SRIBEES</div>
<div style="font-size:13px;opacity:.9">Partner Portal</div>
</td></tr>
<tr><td style="padding:28px 24px">
<p style="margin:0 0 12px;font-size:15px">Hello {safe_name},</p>
<p style="margin:0 0 20px;font-size:15px;line-height:1.5">Use this code to reset your Partner Portal password:</p>
<div style="text-align:center;margin:0 0 20px">
<span style="display:inline-block;font-size:32px;font-weight:bold;letter-spacing:6px;color:{BRAND_COLOR};background:#f0fdfa;border:1px solid #99f6e4;border-radius:8px;padding:12px 20px">{spaced}</span>
</div>
<p style="margin:0 0 12px;font-size:14px;line-height:1.5">It expires in <b>{minutes} minutes</b>. Never share it with anyone &mdash; Sribees staff will never ask for it.</p>
<p style="margin:0;font-size:13px;line-height:1.5;color:#6b7280">If you did not ask to reset your password, ignore this email. Your password stays the same.</p>
</td></tr>
<tr><td style="padding:16px 24px;border-top:1px solid #e5e7eb;font-size:12px;color:#6b7280">
Sribees Online &middot; <a href="mailto:{SUPPORT_EMAIL}" style="color:{BRAND_COLOR}">{SUPPORT_EMAIL}</a>
</td></tr>
</table>
</td></tr></table>
</body></html>"""
    return subject, text, body


# Mail jobs in flight. asyncio keeps only a weak reference to a task, so one
# nobody holds can be collected before it finishes.
_DELIVERIES: set[asyncio.Task] = set()


class PartnerPasswordReset:
    """The three steps. Endpoints call these and nothing else."""

    @staticmethod
    async def request_code(
        db: AsyncSession, redis: Redis, email: str, *, client_ip: Optional[str]
    ) -> None:
        """
        Apply the sending limits, store a fresh code and start mailing it.

        Returns before the mail is sent, and does nothing at all for an
        unknown or deactivated email, so the response is the same in time and
        content either way.
        """
        email = _normalise(email)

        wait_key = _key("wait", email)
        if not await redis.set(wait_key, "1", ex=RESEND_COOLDOWN_SECONDS, nx=True):
            wait = await redis.ttl(wait_key)
            wait = wait if wait and wait > 0 else RESEND_COOLDOWN_SECONDS
            raise reset_error(
                status.HTTP_429_TOO_MANY_REQUESTS,
                "Please wait before requesting another verification code.",
                "OTP_RESEND_TOO_SOON",
                retry_after=wait,
            )

        limits = [(_key("hour", email), MAX_SENDS_PER_EMAIL_PER_HOUR)]
        if client_ip:
            limits.append((f"partner:pwreset:ip:{client_ip}", MAX_SENDS_PER_IP_PER_HOUR))
        for counter, limit in limits:
            count, left = await _count(redis, counter, 3600)
            if count > limit:
                raise reset_error(
                    status.HTTP_429_TOO_MANY_REQUESTS,
                    "Too many verification codes were requested. Please try again later.",
                    "OTP_TOO_MANY_REQUESTS",
                    retry_after=left,
                )

        # A new code replaces the old one, its wrong-guess count and any
        # verified-but-unused reset — for a real address or not.
        await redis.delete(_key("attempts", email), _key("verified", email), _key("code", email))

        partner = await _find_active(db, email)
        if partner is None:
            logger.info(f"Partner password reset asked for unknown/inactive {mask_email(email)}")
            return

        code = f"{secrets.randbelow(10 ** CODE_DIGITS):0{CODE_DIGITS}d}"
        await redis.set(_key("code", email), _hash(code), ex=CODE_TTL_SECONDS)
        task = asyncio.create_task(
            PartnerPasswordReset.deliver(redis, email, partner.full_name, code)
        )
        _DELIVERIES.add(task)
        task.add_done_callback(_DELIVERIES.discard)

    @staticmethod
    async def deliver(redis: Redis, email: str, name: str, code: str) -> None:
        """
        Mail the code. A failed send burns the code and gives back the
        cooldown and the hourly send, so the partner can simply ask again.
        """
        subject, text, body = _email_bodies(name, code)
        try:
            await send_email(email, subject, text, body)
        except EmailSendError as exc:
            logger.error(f"Partner reset code to {mask_email(email)} not sent: {exc}")
            await redis.delete(_key("code", email), _key("wait", email))
            await redis.decr(_key("hour", email))

    @staticmethod
    async def _check_code(redis: Redis, email: str, code: str) -> bool:
        """
        Compare ``code`` with the live one. Every check is counted BEFORE the
        comparison (one atomic INCR), so requests sent at the same moment
        cannot all slip in under the limit; the fifth wrong one burns the code.
        """
        attempts_key = _key("attempts", email)
        await redis.set(attempts_key, 0, ex=CODE_TTL_SECONDS, nx=True)
        attempts = await redis.incr(attempts_key)
        if attempts > MAX_ATTEMPTS:
            await redis.delete(_key("code", email))
            raise too_many_attempts()

        stored = await redis.get(_key("code", email))
        if stored is not None and secrets.compare_digest(str(stored), _hash(code.strip())):
            return True

        if attempts >= MAX_ATTEMPTS:
            await redis.delete(_key("code", email))
            logger.warning(f"Partner reset code burned after {attempts} wrong guesses")
            raise too_many_attempts()
        return False

    @staticmethod
    async def verify(db: AsyncSession, redis: Redis, email: str, code: str) -> str:
        """Check the code and hand back a one-time reset token."""
        email = _normalise(email)
        if not await PartnerPasswordReset._check_code(redis, email, code):
            raise invalid_code()
        if await _find_active(db, email) is None:
            raise invalid_code()

        token = secrets.token_urlsafe(32)
        await redis.set(
            _key("verified", email),
            json.dumps({"code": _hash(code.strip()), "token": _hash(token)}),
            ex=RESET_TOKEN_TTL_SECONDS,
        )
        return token

    @staticmethod
    async def reset(
        db: AsyncSession,
        redis: Redis,
        email: str,
        new_password: str,
        *,
        reset_token: Optional[str],
        code: Optional[str],
    ) -> None:
        """
        Set the new password. Proof is the reset token from verify, or the
        code itself (verified a moment ago, or still live). Either way it is
        good once: everything is deleted on success.
        """
        session_gone = reset_error(
            status.HTTP_400_BAD_REQUEST,
            "Invalid or expired OTP session. Please try again.",
            "OTP_SESSION_INVALID",
        )
        email = _normalise(email)

        raw = await redis.get(_key("verified", email))
        verified = json.loads(raw) if raw else None
        if reset_token:
            ok = verified is not None and secrets.compare_digest(verified["token"], _hash(reset_token))
        elif code:
            ok = (
                verified is not None
                and secrets.compare_digest(verified["code"], _hash(code.strip()))
            ) or await PartnerPasswordReset._check_code(redis, email, code)
        else:
            ok = False
        if not ok:
            raise session_gone

        # Delete first: if two requests race with the same proof, only the
        # one whose delete removed something goes on to change the password.
        removed = await redis.delete(_key("verified", email), _key("code", email))
        await redis.delete(_key("attempts", email))
        partner = await _find_active(db, email)
        if not removed or partner is None:
            raise session_gone

        partner.password_hash = hash_password(new_password)
        partner.password_changed_at = datetime.now(timezone.utc)
        await db.commit()
        logger.info(f"Partner password reset: {mask_email(partner.email)}")
