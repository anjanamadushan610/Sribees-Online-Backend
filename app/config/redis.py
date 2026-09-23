"""
SRIBEESonline FastAPI Backend - Redis Configuration

Async Redis client using redis-py with connection pooling.
"""
from contextlib import asynccontextmanager
from typing import AsyncGenerator, Optional

from redis.asyncio import ConnectionPool, Redis

from app.config.settings import settings
from app.utils.logger import logger

# Global Redis connection pool
_redis_pool: Optional[ConnectionPool] = None
_redis_client: Optional[Redis] = None


async def init_redis() -> Optional[Redis]:
    """
    Initialize Redis connection pool and client.
    Called during application startup.
    On failure, leaves _redis_client None so the app can run without Redis
    (e.g. branch resolve-by-location still works; other features may be limited).
    Returns:
        Redis if connected, None otherwise.
    """
    global _redis_pool, _redis_client

    try:
        _redis_pool = ConnectionPool.from_url(
            settings.redis_connection_url,
            encoding="utf-8",
            decode_responses=True,
            max_connections=20,
        )
        _redis_client = Redis(connection_pool=_redis_pool)

        # Test connection
        await _redis_client.ping()
        logger.info("✅ Redis connected successfully")
        return _redis_client
    except Exception as e:
        logger.warning("Redis connection failed (app will run without Redis): %s", e)
        _redis_client = None
        _redis_pool = None
        return None


async def close_redis() -> None:
    """
    Close Redis connections.

    Called during application shutdown.
    """
    global _redis_pool, _redis_client

    if _redis_client:
        await _redis_client.close()
    if _redis_pool:
        await _redis_pool.disconnect()

    _redis_client = None
    _redis_pool = None
    logger.info("Redis connections closed")


async def get_redis() -> AsyncGenerator[Redis, None]:
    """
    Dependency that provides a Redis client.

    Yields:
        Redis: Async Redis client

    Usage:
        @router.get("/cached-data")
        async def get_cached_data(redis: Redis = Depends(get_redis)):
            data = await redis.get("key")
            ...
    """
    if _redis_client is None:
        raise RuntimeError("Redis not initialized. Call init_redis() first.")
    yield _redis_client


async def get_redis_optional() -> AsyncGenerator[Optional[Redis], None]:
    """
    Dependency that provides a Redis client or None if not initialized.
    Use for endpoints that can work without Redis (e.g. branch resolve-by-location).
    """
    yield _redis_client


@asynccontextmanager
async def get_redis_context() -> AsyncGenerator[Redis, None]:
    """
    Context manager for Redis client outside of request context.

    Usage:
        async with get_redis_context() as redis:
            await redis.set("key", "value")
    """
    if _redis_client is None:
        raise RuntimeError("Redis not initialized. Call init_redis() first.")
    yield _redis_client


def get_redis_client() -> Optional[Redis]:
    """
    Get the current Redis client (sync access).

    Returns:
        Redis: Current Redis client or None if not initialized
    """
    return _redis_client


# ============================================================================
# Redis Key Patterns (matching Express backend)
# ============================================================================

class RedisKeys:
    """
    Redis key patterns for SRIBEESonline.

    Maintains compatibility with Express backend key patterns.
    """

    # Cart keys
    @staticmethod
    def cart(user_id: str) -> str:
        """Cart metadata key."""
        return f"cart:{user_id}"

    @staticmethod
    def cart_items(user_id: str) -> str:
        """Cart items hash key."""
        return f"cart:{user_id}:items"

    # Wishlist keys
    @staticmethod
    def wishlist(user_id: str) -> str:
        """Wishlist set key."""
        return f"wishlist:{user_id}"

    @staticmethod
    def wishlist_details(user_id: str) -> str:
        """Wishlist item details hash."""
        return f"wishlist:{user_id}:details"

    # Session keys
    @staticmethod
    def session(session_id: str) -> str:
        """User session key."""
        return f"session:{session_id}"

    @staticmethod
    def admin_session(session_id: str) -> str:
        """Admin session key."""
        return f"admin_session:{session_id}"

    # Rate limiting keys
    @staticmethod
    def rate_limit(ip: str, endpoint: str) -> str:
        """Rate limit counter key."""
        return f"ratelimit:{ip}:{endpoint}"

    # Cache keys
    @staticmethod
    def product_cache(product_id: str) -> str:
        """Product cache key."""
        return f"product:{product_id}"

    @staticmethod
    def category_cache(category_id: str) -> str:
        """Category cache key."""
        return f"category:{category_id}"

    @staticmethod
    def products_list_cache(page: int, limit: int, filters: str = "") -> str:
        """Products list cache key."""
        return f"products:list:{page}:{limit}:{filters}"

    # Email verification keys
    @staticmethod
    def email_verification(token: str) -> str:
        """Email verification token key."""
        return f"email_verification:{token}"

    # Password reset keys
    @staticmethod
    def password_reset(token: str) -> str:
        """Password reset token key."""
        return f"password_reset:{token}"

    # Push notification tokens
    @staticmethod
    def push_token(user_id: str) -> str:
        """User push notification token."""
        return f"push_token:{user_id}"

    # Branch context keys
    @staticmethod
    def branch_context(user_id: str) -> str:
        """Customer branch-context session key."""
        return f"session:{user_id}:branch_context"

    # App configuration keys
    @staticmethod
    def splash_config() -> str:
        """Splash-screen config cache key."""
        return "app:splash_config"

    # Phone OTP verification key
    @staticmethod
    def phone_otp(user_id: str) -> str:
        """Short-lived phone-verification OTP key (already-authenticated user)."""
        return f"otp:phone:{user_id}"

    @staticmethod
    def account_deletion_otp(user_id: str) -> str:
        """Short-lived OTP key for confirming an account closure.

        Keyed by user, not by phone: the code proves THIS account's owner is
        asking, and a phone-keyed code could be requested by anyone who knows
        the number.
        """
        return f"otp:delete:{user_id}"

    @staticmethod
    def phone_registration_otp(phone: str) -> str:
        """Short-lived OTP key for phone-first registration (no account yet)."""
        return f"otp:register:{phone}"

    # AI shopping chat history key
    @staticmethod
    def chat_history(session_id: str) -> str:
        """
        AI chat conversation history. ``session_id`` is str(user_id) for a
        logged-in shopper or "guest:{device_id}" for a guest — same session
        identity convention branch context uses.
        """
        return f"chat:history:{session_id}"


# ============================================================================
# Redis TTL Constants (in seconds)
# ============================================================================

class RedisTTL:
    """Redis TTL constants."""

    # Cart TTL (30 days)
    CART = 30 * 24 * 60 * 60

    # Wishlist TTL (no expiry, persistent)
    WISHLIST = None

    # Session TTL (7 days default, 30 days for remember me)
    SESSION_DEFAULT = 7 * 24 * 60 * 60
    SESSION_REMEMBER_ME = 30 * 24 * 60 * 60

    # Cache TTL
    PRODUCT_CACHE = 60 * 60  # 1 hour
    CATEGORY_CACHE = 60 * 60  # 1 hour
    PRODUCTS_LIST_CACHE = 5 * 60  # 5 minutes

    # Token TTL
    EMAIL_VERIFICATION = 24 * 60 * 60  # 24 hours
    PASSWORD_RESET = 60 * 60  # 1 hour

    # Phone OTP TTL (3 minutes)
    PHONE_OTP = 3 * 60

    # Branch context TTL (30 days — same as remember-me session)
    BRANCH_CONTEXT = 30 * 24 * 60 * 60

    # AI chat history TTL (7 days — enough for continuity, not indefinite)
    CHAT_HISTORY = 7 * 24 * 60 * 60

    # App configuration cache TTL
    SPLASH_CONFIG = 60 * 60  # 1 hour

    # Rate limit TTL
    RATE_LIMIT = 60  # 1 minute
