"""
SRIBEESonline - SMS status — Super Admin only.

  GET /admin/sms/status — which provider carries the texts, how many went out
                          today against the daily limit, and the account's
                          remaining credit where the provider reports one.

Credentials are never returned: only whether they are set.

Prefix applied by app/api/v1/router.py.
"""
from fastapi import APIRouter, Depends
from redis.asyncio import Redis

from app.config.redis import get_redis
from app.config.settings import settings
from app.core.dependencies import require_roles
from app.services.otp_service import OTPService
from app.services.sms_gateway import SmsSendError, get_sms_gateway

router = APIRouter()

RequireSuperAdmin = Depends(require_roles("super_admin"))


@router.get("/status", response_model=dict, summary="SMS provider, today's count and remaining credit")
async def sms_status(redis: Redis = Depends(get_redis), admin=RequireSuperAdmin) -> dict:
    gateway = get_sms_gateway()
    balance = None
    balance_error = None
    try:
        balance = await gateway.balance()
    except SmsSendError as exc:
        balance_error = str(exc)
    return {
        "provider": gateway.name,
        "sender_id": settings.notifylk_sender_id if gateway.name == "notifylk" else None,
        "sent_today": await OTPService.sent_today(redis),
        "daily_limit": settings.sms_daily_limit,
        "balance": balance,
        "balance_error": balance_error,
    }
