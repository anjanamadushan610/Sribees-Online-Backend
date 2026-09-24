"""
SRIBEESonline - Admin Settings API

Super-Admin endpoints for managing application-wide settings.

  GET   /                — retrieve platform settings (pricing + app config)
  PATCH /                — update platform settings
  POST  /splash-video    — upload a new splash video to S3 & save URL
  GET   /splash-video    — retrieve current splash video details

  GET    /splash-animation — the Lottie splash the mobile app plays
  POST   /splash-animation — upload a new Lottie JSON (goes live immediately)
  PATCH  /splash-animation — switch it on/off or change its backdrop colour
  DELETE /splash-animation — remove it; the app returns to the built-in splash
"""
import io
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from loguru import logger
from pydantic import BaseModel, Field
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.app_public import APP_CONFIG_CACHE_KEY
from app.config.database import get_db
from app.config.redis import get_redis
from app.config.settings import settings
from app.core.dependencies import require_roles
from app.core.media import media_url
from app.schemas.app_settings import (
    PlatformSettings,
    PlatformSettingsResponse,
    PlatformSettingsUpdate,
    SplashVideoResponse,
)
from app.services import splash_animation
from app.services.app_settings_service import AppSettingsService
from app.services.storage import StorageError, get_storage

router = APIRouter()

RequireSuperAdmin = Depends(require_roles("super_admin"))

# Allowed video MIME types
ALLOWED_VIDEO_TYPES = {
    "video/mp4",
    "video/quicktime",
    "video/x-msvideo",
    "video/webm",
    "video/x-matroska",
}

# Redis key for the cached splash config
SPLASH_CACHE_KEY = "app:splash_config"
SPLASH_CACHE_TTL = 60 * 60  # 1 hour


# ============================================================================
# Platform Settings — GET / PATCH
# ============================================================================

@router.get(
    "",
    response_model=PlatformSettingsResponse,
    summary="Get platform settings",
    description="Retrieve checkout pricing and mobile-app configuration.",
)
async def get_platform_settings(
    db: AsyncSession = Depends(get_db),
    admin=RequireSuperAdmin,
):
    values = await AppSettingsService.get_platform_settings(db)
    return PlatformSettingsResponse(success=True, data=PlatformSettings(**values))


@router.patch(
    "",
    response_model=PlatformSettingsResponse,
    summary="Update platform settings",
    description=(
        "Update any of: order tax rate (%), splash video URL. The delivery charge is "
        "not a setting — SribeesExpress quotes it per parcel."
    ),
)
async def update_platform_settings(
    data: PlatformSettingsUpdate,
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
    admin=RequireSuperAdmin,
):
    values = await AppSettingsService.update_platform_settings(
        db,
        order_tax_rate_percent=data.order_tax_rate_percent,
        splash_video_url=data.splash_video_url,
    )

    # If the splash URL changed, drop the cached public splash config so the
    # Flutter app picks up the new value immediately.
    if data.splash_video_url is not None:
        try:
            await redis.delete(SPLASH_CACHE_KEY)
        except Exception:  # pragma: no cover - cache invalidation is best-effort
            logger.warning("Could not invalidate splash cache after settings update")

    logger.info("Platform settings updated")
    return PlatformSettingsResponse(success=True, data=PlatformSettings(**values))


def _format_splash_response(setting) -> SplashVideoResponse:
    """Format an AppSetting row into a SplashVideoResponse."""
    return SplashVideoResponse(
        setting_id=setting.setting_id,
        key=setting.key,
        # Stored as a relative path; resolved to an absolute URL at the edge.
        video_url=media_url(setting.value),
        description=setting.description,
        is_active=setting.is_active,
        updated_at=setting.updated_at,
    )


# ============================================================================
# GET /splash-video
# ============================================================================

@router.get(
    "/splash-video",
    response_model=dict,
    summary="Get current splash video details",
    description="Returns the current splash-screen video configuration.",
)
async def get_splash_video(
    db: AsyncSession = Depends(get_db),
    admin=RequireSuperAdmin,
):
    setting = await AppSettingsService.get_splash_video(db)
    if setting is None:
        return {
            "success": True,
            "data": {
                "key": "splash_video_url",
                "video_url": None,
                "is_active": False,
                "description": "No splash video configured yet",
                "updated_at": None,
            },
        }

    return {
        "success": True,
        "data": _format_splash_response(setting).model_dump(mode="json"),
    }


# ============================================================================
# POST /splash-video
# ============================================================================

@router.post(
    "/splash-video",
    response_model=dict,
    summary="Upload a new splash video",
    description=(
        "Upload a video file (MP4, WebM, etc.) to S3 and save the URL "
        "in the ``app_settings`` table as the splash-screen video."
    ),
)
async def upload_splash_video(
    file: UploadFile = File(..., description="Video file (MP4 recommended, max 50 MB)"),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
    admin=RequireSuperAdmin,
):
    # Validate content type
    if file.content_type not in ALLOWED_VIDEO_TYPES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "success": False,
                "error": {
                    "message": (
                        f"Invalid file type '{file.content_type}'. "
                        f"Allowed: {', '.join(sorted(ALLOWED_VIDEO_TYPES))}"
                    ),
                    "code": "INVALID_FILE_TYPE",
                },
            },
        )

    # Validate size (read into memory for small splash videos)
    max_bytes = settings.s3_max_upload_size_mb * 1024 * 1024
    contents = await file.read()
    if len(contents) > max_bytes:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail={
                "success": False,
                "error": {
                    "message": (
                        f"File too large ({len(contents) / 1024 / 1024:.1f} MB). "
                        f"Maximum allowed: {settings.s3_max_upload_size_mb} MB."
                    ),
                    "code": "FILE_TOO_LARGE",
                },
            },
        )

    # Store through the configured StorageProvider (local disk today, S3 later).
    fileobj = io.BytesIO(contents)

    try:
        path = get_storage().save(
            fileobj=fileobj,
            filename=file.filename or "splash_video.mp4",
            folder="splash",
            content_type=file.content_type,
        )
    except StorageError as exc:
        # 500, not 502 — see admin_catalog._upload_image. The storage backend is
        # in-process, so this is a server fault, not an upstream gateway's.
        logger.error(f"Splash video upload failed: {exc}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={
                "success": False,
                "error": {
                    "message": "Could not store the video on the server. Please try again.",
                    "code": "STORAGE_UPLOAD_FAILED",
                },
            },
        )

    # Replace the previous video's file. Read the old value BEFORE overwriting
    # the setting, or its path is gone and the file is orphaned forever.
    old_setting = await AppSettingsService.get_splash_video(db)
    old_value = old_setting.value if old_setting else None

    # The DB stores the relative path only; the absolute URL is composed on read.
    setting = await AppSettingsService.set_splash_video_url(db, path, is_active=True)

    if old_value and old_value != path:
        try:
            get_storage().delete(old_value)
        except Exception:
            logger.warning("Could not delete the previous splash video — continuing")

    # Invalidate Redis cache
    await redis.delete(SPLASH_CACHE_KEY)
    logger.info(f"Splash video updated: {path}")

    return {
        "success": True,
        "data": _format_splash_response(setting).model_dump(mode="json"),
        "message": "Splash video uploaded successfully",
    }


# ============================================================================
# Splash animation (Lottie JSON)
# ============================================================================


def _error(status_code: int, message: str, code: str) -> HTTPException:
    return HTTPException(
        status_code=status_code,
        detail={"success": False, "error": {"message": message, "code": code}},
    )


def _splash_payload(value: Optional[dict], setting) -> Optional[dict]:
    if not value:
        return None
    return {
        **{k: v for k, v in value.items() if k != "path"},
        "animation_url": media_url(value["path"]),
        "is_active": bool(setting and setting.is_active),
        "updated_at": setting.updated_at.isoformat() if setting and setting.updated_at else None,
    }


async def _forget_cached_app_config(redis: Redis) -> None:
    # The config is cached for 60s; dropping it means the next launch sees the
    # change rather than the one after.
    try:
        await redis.delete(APP_CONFIG_CACHE_KEY)
    except Exception:  # pragma: no cover - cache invalidation is best-effort
        logger.warning("Could not invalidate the app-config cache")


@router.get(
    "/splash-animation",
    response_model=dict,
    summary="Get the mobile app's splash animation",
)
async def get_splash_animation(
    db: AsyncSession = Depends(get_db),
    admin=RequireSuperAdmin,
):
    value, setting = await splash_animation.get_splash_animation(db)
    return {"success": True, "data": _splash_payload(value, setting)}


@router.post(
    "/splash-animation",
    response_model=dict,
    summary="Upload a new splash animation (Lottie JSON)",
    description=(
        "Validates the Lottie, stores it and makes it live. Installed apps "
        "download it on their next launch or resume and play it from the "
        "launch after that."
    ),
)
async def upload_splash_animation(
    file: UploadFile = File(..., description="Lottie animation exported as JSON"),
    background_color: Optional[str] = Form(None),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
    admin=RequireSuperAdmin,
):
    # Read one byte past the limit: enough to know it is too big without
    # holding an arbitrarily large upload in memory.
    raw = await file.read(splash_animation.MAX_BYTES + 1)
    try:
        meta = splash_animation.inspect_lottie(raw)
        color = splash_animation.normalise_hex_color(background_color)
    except splash_animation.SplashAnimationError as exc:
        code = (
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE
            if exc.code == "FILE_TOO_LARGE"
            else status.HTTP_400_BAD_REQUEST
        )
        raise _error(code, str(exc), exc.code)

    try:
        path = get_storage().save(
            fileobj=io.BytesIO(raw),
            # The stored name always ends in .json so it is served as JSON,
            # whatever the admin's file happened to be called.
            filename="splash_animation.json",
            folder="splash",
            content_type="application/json",
        )
    except StorageError as exc:
        logger.error(f"Splash animation upload failed: {exc}")
        raise _error(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "Could not store the animation on the server. Please try again.",
            "STORAGE_UPLOAD_FAILED",
        )

    # Read the old path BEFORE overwriting the row, or its file is orphaned.
    old_value, _ = await splash_animation.get_splash_animation(db)

    value = splash_animation.build_value(
        path, meta, filename=file.filename, background_color=color
    )
    setting = await splash_animation.save_splash_animation(db, value, is_active=True)

    # Deleting the old file is safe even for a phone mid-download: it compares
    # versions, sees the new one on its next config fetch and downloads that.
    if old_value and old_value.get("path") != path:
        try:
            get_storage().delete(old_value["path"])
        except Exception:
            logger.warning("Could not delete the previous splash animation - continuing")

    await _forget_cached_app_config(redis)
    logger.info(f"Splash animation updated: {path} ({meta['bytes']} bytes)")
    return {
        "success": True,
        "data": _splash_payload(value, setting),
        "message": "Splash animation is live",
    }


class SplashAnimationUpdate(BaseModel):
    model_config = {"extra": "forbid"}

    is_active: Optional[bool] = None
    background_color: Optional[str] = Field(None, max_length=7)


@router.patch(
    "/splash-animation",
    response_model=dict,
    summary="Switch the splash animation on/off or change its backdrop",
)
async def update_splash_animation(
    data: SplashAnimationUpdate,
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
    admin=RequireSuperAdmin,
):
    value, setting = await splash_animation.get_splash_animation(db)
    if not value:
        raise _error(
            status.HTTP_404_NOT_FOUND,
            "No splash animation has been uploaded yet.",
            "NOT_FOUND",
        )

    if data.background_color is not None:
        try:
            value["background_color"] = splash_animation.normalise_hex_color(
                data.background_color
            )
        except splash_animation.SplashAnimationError as exc:
            raise _error(status.HTTP_400_BAD_REQUEST, str(exc), exc.code)

    is_active = setting.is_active if data.is_active is None else data.is_active
    setting = await splash_animation.save_splash_animation(db, value, is_active=is_active)

    await _forget_cached_app_config(redis)
    return {"success": True, "data": _splash_payload(value, setting)}


@router.delete(
    "/splash-animation",
    response_model=dict,
    summary="Remove the splash animation (apps return to the built-in splash)",
)
async def delete_splash_animation(
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
    admin=RequireSuperAdmin,
):
    value, setting = await splash_animation.get_splash_animation(db)
    if setting is not None:
        await AppSettingsService.upsert(
            db, key=splash_animation.SPLASH_ANIMATION_KEY, value=None, is_active=False
        )
    if value:
        try:
            get_storage().delete(value["path"])
        except Exception:
            logger.warning("Could not delete the splash animation file - continuing")

    await _forget_cached_app_config(redis)
    return {"success": True, "data": None, "message": "Splash animation removed"}
