"""
SRIBEESonline - Public App Configuration API

Unauthenticated endpoints that the Flutter client calls on every app
launch.  Responses are aggressively cached in Redis.

  GET /splash-config  — returns the current splash video URL + active flag
  GET /app-config     — the remote controls for an already-shipped build,
                        including the Lottie splash animation (if any)

Mobile connectivity:
  The Flutter client should send ``X-Client-Platform: android-emulator``
  when running inside the Android emulator.  The endpoint will rewrite
  S3 URLs from ``localhost`` to ``10.0.2.2`` so the emulator can reach
  the local MinIO instance.
"""
import json
from typing import Optional

from fastapi import APIRouter, Depends, Header
from loguru import logger
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.database import get_db
from app.config.redis import get_redis
from app.core.media import media_url_for_client
from app.services.app_settings_service import AppSettingsService

router = APIRouter()

# Redis cache key & TTL for the splash config
SPLASH_CACHE_KEY = "app:splash_config"
SPLASH_CACHE_TTL = 60 * 60  # 1 hour

# The mobile remote config. Cached far more briefly than the splash video on
# purpose: this is the lever that gets pulled DURING an incident, and an hour
# of staleness would mean an hour of every installed copy ignoring the kill
# switch that was just thrown.
APP_CONFIG_CACHE_KEY = "app:mobile_config"
APP_CONFIG_CACHE_TTL = 60  # seconds


# ============================================================================
# GET /splash-config
# ============================================================================

@router.get(
    "/splash-config",
    response_model=dict,
    summary="Get splash screen configuration",
    description=(
        "Public endpoint called by the Flutter app on every launch. "
        "Returns the splash-video URL and whether it is active. "
        "Heavily cached in Redis to minimise database load.\n\n"
        "**Mobile tip**: Send header ``X-Client-Platform: android-emulator`` "
        "from the Android emulator so the URL is rewritten from "
        "``localhost`` to ``10.0.2.2``."
    ),
)
async def get_splash_config(
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
    x_client_platform: Optional[str] = Header(default=None),
):
    # 1. Try Redis cache first (stores the canonical URL)
    cached = await redis.get(SPLASH_CACHE_KEY)
    if cached:
        payload = json.loads(cached)
        logger.debug("Splash config served from Redis cache")
    else:
        # 2. Cache miss — query the database
        setting = await AppSettingsService.get_splash_video(db)

        if setting and setting.is_active and setting.value:
            payload = {
                "success": True,
                "data": {
                    "splash_video_url": setting.value,
                    "is_active": True,
                },
            }
        else:
            payload = {
                "success": True,
                "data": {
                    "splash_video_url": None,
                    "is_active": False,
                },
            }

        # 3. Store canonical payload in Redis
        await redis.setex(SPLASH_CACHE_KEY, SPLASH_CACHE_TTL, json.dumps(payload))
        logger.debug("Splash config cached in Redis")

    # 4. Resolve the stored path into an absolute URL for THIS client.
    #
    # The cache deliberately holds the raw stored value (a relative path), not a
    # resolved URL: resolution happens per-request, so changing MEDIA_BASE_URL —
    # or moving to S3 — takes effect immediately without flushing Redis, and one
    # client's emulator rewrite can never be cached and served to another.
    raw_value = payload.get("data", {}).get("splash_video_url")
    resolved = media_url_for_client(raw_value, x_client_platform)
    if resolved != raw_value:
        payload = {
            **payload,
            "data": {
                **payload["data"],
                "splash_video_url": resolved,
            },
        }

    return payload


# ============================================================================
# GET /support-info
# ============================================================================

@router.get(
    "/support-info",
    response_model=dict,
    summary="Get customer support contact info",
    description=(
        "Public endpoint for the app's Help & Support screen. Returns the "
        "support phone number and email configured in app_settings."
    ),
)
async def get_support_info(db: AsyncSession = Depends(get_db)):
    phone_setting = await AppSettingsService.get_by_key(db, "support_phone")
    email_setting = await AppSettingsService.get_by_key(db, "support_email")

    return {
        "success": True,
        "data": {
            "supportPhone": phone_setting.value if phone_setting else None,
            "supportEmail": email_setting.value if email_setting else None,
        },
    }


# ============================================================================
# GET /app-config
# ============================================================================

@router.get(
    "/app-config",
    response_model=dict,
    summary="Remote configuration for the installed mobile app",
    description=(
        "Unauthenticated, called by the Flutter client at launch and on "
        "resume. "
        "Exists because the Play Store freezes the app: once a build is "
        "submitted, changing anything inside it means another review. These "
        "values are the only way to steer a copy already on someone's phone "
        "-- require an update, put up a maintenance notice, or switch off a "
        "feature that turned out to be broken."
    ),
)
async def get_app_config(
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
    x_client_platform: Optional[str] = Header(default=None),
):
    cached = await redis.get(APP_CONFIG_CACHE_KEY)
    if cached:
        try:
            data = json.loads(cached)
            return {
                "success": True,
                "data": _for_client(data, x_client_platform),
                "cached": True,
            }
        except ValueError:
            # A corrupt cache entry is not a reason to fail the request that
            # tells the app whether it may run at all. Fall through and read
            # the database.
            logger.warning("app:mobile_config cache entry was not valid JSON")

    try:
        data = await AppSettingsService.get_mobile_config(db)
    except Exception as exc:
        # This endpoint gates the whole app. If the database is unreachable,
        # answering "carry on normally" is far better than answering 500 and
        # having every installed copy decide it cannot start -- an outage of
        # the settings table would otherwise become an outage of the product.
        logger.error(f"app-config could not be read, serving permissive defaults: {exc}")
        return {
            "success": True,
            "data": {
                "minimum_version": "0.0.0",
                "latest_version": None,
                "update_url": None,
                "maintenance": {"active": False, "message": None},
                "flags": {},
                "splash": None,
            },
            "degraded": True,
        }

    await redis.setex(APP_CONFIG_CACHE_KEY, APP_CONFIG_CACHE_TTL, json.dumps(data))
    return {
        "success": True,
        "data": _for_client(data, x_client_platform),
        "cached": False,
    }


def _for_client(data: dict, client_platform: Optional[str]) -> dict:
    """
    Turn the cached splash ``path`` into a URL for THIS client. The cache keeps
    the relative path so an emulator's rewritten URL is never served to a phone
    (same reasoning as /splash-config above).
    """
    splash = data.get("splash")
    if not isinstance(splash, dict) or not splash.get("path"):
        return {**data, "splash": None}
    public = {k: v for k, v in splash.items() if k != "path"}
    public["animation_url"] = media_url_for_client(splash["path"], client_platform)
    return {**data, "splash": public}
