"""
SRIBEESonline - Admin Settings API

Super-Admin endpoints for managing application-wide settings.

  GET   /                — retrieve platform settings (pricing + app config)
  PATCH /                — update platform settings
  POST  /splash-video    — upload a new splash video to S3 & save URL
  GET   /splash-video    — retrieve current splash video details
"""
from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from loguru import logger
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

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
    description="Update any of: flat delivery fee, order tax rate (%), splash video URL.",
)
async def update_platform_settings(
    data: PlatformSettingsUpdate,
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
    admin=RequireSuperAdmin,
):
    values = await AppSettingsService.update_platform_settings(
        db,
        flat_delivery_fee=data.flat_delivery_fee,
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
    import io
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
