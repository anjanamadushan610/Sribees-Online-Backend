"""
SRIBEESonline FastAPI Backend - Main Application

FastAPI application entry point with lifespan management.
Includes Sentry error tracking for production monitoring.
"""

from contextlib import asynccontextmanager
from typing import AsyncGenerator

import sentry_sdk
from fastapi import FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sentry_sdk.integrations.fastapi import FastApiIntegration
from sentry_sdk.integrations.redis import RedisIntegration
from sentry_sdk.integrations.sqlalchemy import SqlalchemyIntegration

from app.api.share_links import router as share_links_router
from app.api.v1.router import router as v1_router
from app.config.database import close_db, init_db
from app.config.redis import close_redis, init_redis
from app.config.settings import settings
from app.core.exceptions import AppException
from app.services.fcm_service import FCMService
from app.utils.logger import logger


def init_sentry() -> None:
    """Initialize Sentry error tracking for production."""
    if settings.sentry_dsn:
        sentry_sdk.init(
            dsn=settings.sentry_dsn,
            environment=settings.app_env,
            release="sribeesonline-api@1.0.0",
            traces_sample_rate=settings.sentry_traces_sample_rate,
            profiles_sample_rate=settings.sentry_profiles_sample_rate,
            integrations=[
                FastApiIntegration(transaction_style="endpoint"),
                SqlalchemyIntegration(),
                RedisIntegration(),
            ],
            send_default_pii=False,  # Don't send personal data
        )
        logger.info(f"Sentry initialized for environment: {settings.app_env}")
    else:
        logger.warning("Sentry DSN not configured - error tracking disabled")


def init_fcm() -> None:
    """Initialize Firebase Cloud Messaging for push notifications.

    Either source will do: the JSON itself in the environment (preferred in a
    container) or a path to a file. This gate used to check only the path, so
    configuring the JSON left push silently disabled with FCMService.initialize
    never even called -- the log said "not configured" while the credential sat
    right there in the environment.
    """
    if settings.firebase_credentials_json or settings.firebase_credentials_path:
        if FCMService.initialize():
            logger.info("Firebase Cloud Messaging initialized")
        else:
            logger.warning("FCM initialization failed - push notifications disabled")
    else:
        logger.info("Firebase credentials not configured - FCM disabled")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator:
    """
    Application lifespan manager.

    Handles startup and shutdown events.
    """
    # Startup
    logger.info(f"Starting {settings.app_name} v{settings.api_version}")
    logger.info(f"Environment: {settings.app_env}")

    try:
        # Initialize Sentry error tracking
        init_sentry()

        # Initialize Firebase Cloud Messaging
        init_fcm()

        # Initialize database connection
        await init_db()

        # Initialize Redis connection
        await init_redis()

        # Tokenise any product the search index hasn't seen (new rows written
        # by raw SQL, or all of them after search_text.INDEX_VERSION changes).
        # Never fatal: a failure leaves search on the old tokens, not down.
        try:
            from app.config.database import async_session_maker
            from app.services.catalog_search_service import reindex_stale

            reindexed = await reindex_stale(async_session_maker)
            if reindexed:
                logger.info(f"Search index: tokenised {reindexed} products")
        except Exception as e:
            logger.warning(f"Search index refresh skipped: {e}")
        try:
            from app.services.search_insights_service import prune

            pruned = await prune(async_session_maker)
            if pruned:
                logger.info(f"Search misses: pruned {pruned} stale one-off rows")
        except Exception as e:
            logger.warning(f"Search miss pruning skipped: {e}")

        logger.info("Application startup complete")

    except Exception as e:
        logger.error(f"Startup failed: {e}")
        sentry_sdk.capture_exception(e)
        raise

    import asyncio
    from contextlib import suppress

    from app.services.assistant_reminder_worker import run_reminder_worker
    from app.services.order_reward_worker import run_order_reward_worker
    from app.services.referral_vesting_worker import run_referral_vesting_worker

    # Separate loops, not one. They fail for unrelated reasons -- reminders,
    # commissions and order rewards each need their own tables -- and sharing
    # a task would mean an exception in any stopping all. A money sweep that
    # silently stopped looks exactly like "the money never vested", which is
    # the one failure nobody reports as a bug.
    background = [
        asyncio.create_task(run_reminder_worker()),
        asyncio.create_task(run_referral_vesting_worker()),
        asyncio.create_task(run_order_reward_worker()),
    ]
    try:
        yield
    finally:
        for task in background:
            task.cancel()
        for task in background:
            with suppress(asyncio.CancelledError):
                await task

    # Shutdown
    logger.info("Shutting down application...")

    await close_redis()
    await close_db()

    logger.info("Application shutdown complete")


# Create FastAPI application
app = FastAPI(
    title=settings.app_name,
    description="SRIBEESonline E-Commerce API - FastAPI Implementation",
    version="1.0.0",
    docs_url="/docs" if settings.debug else None,
    redoc_url="/redoc" if settings.debug else None,
    openapi_url="/openapi.json" if settings.debug else None,
    lifespan=lifespan,
)


# ============================================================================
# Middleware
# ============================================================================

# Security middleware (headers, request ID, rate limiting)
from app.core.rate_limiter import RateLimitMiddleware
from app.core.security_headers import configure_security_middleware

configure_security_middleware(app)
app.add_middleware(RateLimitMiddleware)


# Request logging middleware
@app.middleware("http")
async def log_requests(request: Request, call_next):
    """Log all incoming requests."""
    request_id = getattr(request.state, "request_id", "unknown")
    logger.info(
        f"{request.method} {request.url.path}",
        extra={
            "request_id": request_id,
            "ip": request.client.host if request.client else "unknown",
            "user_agent": request.headers.get("user-agent", "unknown"),
        },
    )
    response = await call_next(request)
    return response


# CORS middleware — added LAST so it is the OUTERMOST middleware. This
# guarantees every response, including rate-limit rejections and error
# responses from inner middleware, carries the Access-Control-Allow-Origin
# header (otherwise the browser reports a real 4xx/5xx as an opaque CORS error).
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_origin_regex=r"https://.*\.vercel\.app|https://([a-z0-9-]+\.)*sribees\.com",
    allow_credentials=settings.cors_allow_credentials,
    allow_methods=settings.cors_allow_methods,
    allow_headers=settings.cors_allow_headers,
)


# ============================================================================
# Exception Handlers
# ============================================================================


@app.exception_handler(AppException)
async def app_exception_handler(request: Request, exc: AppException):
    """Handle application exceptions."""
    return JSONResponse(
        status_code=exc.status_code,
        content=exc.detail,
    )


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    """Handle unhandled exceptions."""
    logger.exception("Unhandled exception: %s", exc)

    content = {
        "success": False,
        "error": {
            "message": "An unexpected error occurred",
            "code": "INTERNAL_ERROR",
        },
    }
    # In debug mode, include exception details so clients can see the real error
    if getattr(settings, "debug", False):
        content["error"]["detail"] = str(exc)
        content["error"]["type"] = type(exc).__name__

    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content=content,
    )


# ============================================================================
# Routes
# ============================================================================


# Health check endpoint
@app.get("/health", tags=["Health"])
async def health_check():
    """Health check endpoint."""
    return {
        "success": True,
        "message": "SRIBEESonline API is running",
        "timestamp": __import__("datetime").datetime.utcnow().isoformat(),
        "environment": settings.app_env,
        "version": "1.0.0",
    }


# API root endpoint
@app.get("/api", tags=["API Info"])
async def api_info():
    """API information endpoint."""
    return {
        "success": True,
        "message": "SRIBEESonline API",
        "version": settings.api_version,
        "endpoints": {
            "auth": f"/api/{settings.api_version}/auth",
            "adminAuth": f"/api/{settings.api_version}/admin/auth",
            "products": f"/api/{settings.api_version}/products",
            "categories": f"/api/{settings.api_version}/categories",
            "cart": f"/api/{settings.api_version}/cart",
            "wishlist": f"/api/{settings.api_version}/wishlist",
            "orders": f"/api/{settings.api_version}/orders",
            "deliverySlots": f"/api/{settings.api_version}/delivery/slots",
            "payments": f"/api/{settings.api_version}/payments",
            "notifications": f"/api/{settings.api_version}/notifications",
            "branch": f"/api/{settings.api_version}/branch",
            "locations": f"/api/{settings.api_version}/locations",
            "adminLocations": f"/api/{settings.api_version}/admin/locations",
            "inventory": f"/api/{settings.api_version}/inventory",
            "adminSettings": f"/api/{settings.api_version}/admin/settings",
            "marketing": f"/api/{settings.api_version}/admin/marketing",
            "appConfig": f"/api/{settings.api_version}/app",
        },
        "documentation": "/docs" if settings.debug else "Disabled in production",
    }


# Include API v1 router
app.include_router(v1_router, prefix="/api")

# Product share links + Android App Links proof (site root, see share_links.py)
app.include_router(share_links_router)


# ============================================================================
# Media (local disk storage backend)
# ============================================================================
# Serve uploaded media when the local provider is active. With STORAGE_BACKEND=s3
# the files live in the bucket and are fetched straight from MEDIA_BASE_URL, so
# nothing is mounted here — the app never proxies them.
if (settings.storage_backend or "local").strip().lower() == "local":
    import mimetypes
    from pathlib import Path

    from fastapi.staticfiles import StaticFiles

    # StaticFiles picks the response Content-Type from the extension via
    # mimetypes.guess_type(), which serves unrecognized extensions as
    # text/plain. Combined with the X-Content-Type-Options: nosniff security
    # header, browsers then refuse to render the image at all — it 200s but
    # shows as a broken <img>. Confirmed in prod: .jfif (a common "Save Image"
    # extension from browsers/Google Images) isn't in Python 3.11's table on
    # Linux. Uploads keep whatever extension the browser handed us (see
    # LocalDiskStorageProvider.save), so every extension we accept for
    # uploads (see ALLOWED_IMAGE_TYPES) is registered explicitly here rather
    # than trusting the interpreter's table.
    for _ext, _mime in (
        (".jfif", "image/jpeg"),
        (".jpg", "image/jpeg"),
        (".jpeg", "image/jpeg"),
        (".png", "image/png"),
        (".webp", "image/webp"),
        (".gif", "image/gif"),
    ):
        mimetypes.add_type(_mime, _ext)

    _media_root = Path(settings.media_root)
    # StaticFiles refuses to start if the directory is missing, which on a fresh
    # volume it always is.
    #
    # Creating it can itself fail — a read-only mount, a locked-down host, or a
    # CI runner where the configured path (`/app/media`) is simply not ours to
    # make. That must not take the module down: this runs at IMPORT time, so an
    # unhandled error here means `import app.main` fails and nothing works, not
    # even the routes that never touch an upload. Same philosophy as the
    # writability probe below — say so loudly, then carry on without the mount.
    _media_ready = True
    try:
        _media_root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        _media_ready = False
        logger.error(
            f"MEDIA_ROOT {_media_root} could not be created ({exc}). Uploads "
            f"and /{settings.media_url_prefix.strip('/')} are DISABLED for this "
            f"process; every other route still works. Set MEDIA_ROOT to a path "
            f"this process may write to."
        )

    # Fail LOUDLY at boot if the directory is not writable, instead of letting
    # the first admin upload discover it as an opaque 5xx. This is exactly how
    # the root-owned-volume bug hid: the app was healthy, the mount existed, and
    # only the write failed — hours later, in someone else's browser console.
    _probe = _media_root / ".write-probe"
    try:
        if not _media_ready:
            raise OSError("media root does not exist")
        _probe.write_bytes(b"ok")
        _probe.unlink(missing_ok=True)
        logger.info(
            f"Serving media from {_media_root} at "
            f"/{settings.media_url_prefix.strip('/')} (writable)"
        )
    except OSError as exc:
        logger.error(
            f"MEDIA_ROOT {_media_root} is NOT WRITABLE ({exc}). Uploads will "
            f"fail. If this is a Docker volume, its mount point is most likely "
            f"owned by root while the app runs as a non-root user — ensure the "
            f"directory exists in the image and is chowned to the app user."
        )

    # StaticFiles would raise on a directory that is not there, so only mount
    # when we actually have one.
    if _media_ready:
        app.mount(
            f"/{settings.media_url_prefix.strip('/')}",
            StaticFiles(directory=str(_media_root)),
            name="media",
        )


# ============================================================================
# Development Server
# ============================================================================

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        reload=settings.debug,
        workers=1 if settings.debug else settings.workers,
        log_level=settings.log_level.lower(),
    )
