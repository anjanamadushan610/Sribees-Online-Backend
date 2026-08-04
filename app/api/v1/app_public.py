"""
SRIBEESonline - Public App Configuration API

Unauthenticated endpoints that the Flutter client calls on every app
launch.  Responses are aggressively cached in Redis.

  GET /splash-config  — returns the current splash video URL + active flag

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
