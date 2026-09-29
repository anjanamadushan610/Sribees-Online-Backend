"""
SRIBEESonline - SMS — Super Admin only.

  GET /admin/sms           — which provider carries the texts, whether its
                             credentials are set, the remaining credit, today's
                             count against the daily total, the last 7 days, and
                             the sending rules in force.
  PUT /admin/sms/settings  — the daily total for the whole server (0 = no texts).

The per-number and per-address rules are code (OTPService), the same on every
server; only the daily total is a setting. Credentials are never returned:
only whether they are set.

Prefix applied by app/api/v1/router.py.
"""
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.database import get_db
from app.config.redis import RedisTTL, get_redis
from app.config.settings import settings
from app.core.dependencies import get_client_ip, require_roles
from app.services.app_settings_service import SMS_DAILY_LIMIT_KEY, AppSettingsService
from app.services.audit_service import AuditAction, AuditService
from app.services.otp_service import OTPService
from app.services.sms_gateway import SmsSendError, UnconfiguredGateway, get_sms_gateway

router = APIRouter()

RequireSuperAdmin = Depends(require_roles("super_admin"))

MAX_DAILY_LIMIT = 10_000


class SmsSettingsBody(BaseModel):
    model_config = {"extra": "forbid"}
    daily_limit: int = Field(..., ge=0, le=MAX_DAILY_LIMIT)


async def _overview(db: AsyncSession, redis: Redis) -> dict:
    gateway = get_sms_gateway()
    balance = None
    balance_error = None
    if gateway.name != "console":
        try:
            balance = await gateway.balance()
        except SmsSendError as exc:
            balance_error = str(exc)
    daily_limit = await AppSettingsService.get_sms_daily_limit(db)
    sent_today = await OTPService.sent_today(redis)
    return {
        "provider": gateway.name,
        # "console" means texts only reach the server log: nobody gets a code.
        "is_live": gateway.name != "console" and not isinstance(gateway, UnconfiguredGateway),
        # Names of missing settings, never values.
        "missing": gateway.missing if isinstance(gateway, UnconfiguredGateway) else [],
        "sender_id": settings.textlk_sender_id if gateway.name == "textlk" else None,
        "balance": balance,
        "balance_error": balance_error,
        "daily_limit": daily_limit,
        "sent_today": sent_today,
        "remaining_today": max(0, daily_limit - sent_today),
        "history": await OTPService.sent_by_day(redis, 7),
        "rules": {
            "resend_wait_seconds": OTPService.RESEND_COOLDOWN_SECONDS,
            "per_number_per_hour": OTPService.MAX_SENDS_PER_PHONE_PER_HOUR,
            "per_number_per_day": OTPService.MAX_SENDS_PER_PHONE_PER_DAY,
            "per_address_per_hour": OTPService.MAX_SENDS_PER_IP_PER_HOUR,
            "code_valid_seconds": RedisTTL.PHONE_OTP,
            "wrong_tries_per_code": OTPService.MAX_ATTEMPTS,
        },
    }


@router.get("", response_model=dict, summary="SMS provider, credit, today's count and the rules")
async def sms_overview(
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
    admin=RequireSuperAdmin,
) -> dict:
    return {"success": True, "data": await _overview(db, redis)}


@router.put("/settings", response_model=dict, summary="Set the daily total of texts (0 = none)")
async def update_sms_settings(
    body: SmsSettingsBody,
    request: Request,
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
    admin=RequireSuperAdmin,
) -> dict:
    before = await AppSettingsService.get_sms_daily_limit(db)
    await AppSettingsService.upsert(
        db,
        SMS_DAILY_LIMIT_KEY,
        str(body.daily_limit),
        description="Texts the server may send per Sri Lankan day, all numbers together (0 = none)",
    )
    await AuditService.log_admin_action(
        db=db,
        action=AuditAction.SYSTEM_CONFIG_CHANGE,
        admin_id=admin.admin_id,
        admin_email=admin.email,
        target_type="app_setting",
        target_id=None,
        description=f"SMS daily limit {before} -> {body.daily_limit}",
        ip_address=get_client_ip(request),
        changes={"key": SMS_DAILY_LIMIT_KEY, "before": before, "after": body.daily_limit},
    )
    return {"success": True, "data": await _overview(db, redis)}
