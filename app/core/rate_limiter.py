"""
SRIBEESonline - Rate Limiting Middleware

Redis-based rate limiting with multiple strategies.
Protects API endpoints from abuse and DDoS attacks.
"""
import hashlib
from datetime import datetime
from typing import Callable, Optional

from fastapi import HTTPException, Request, status
from loguru import logger
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse, Response

from app.config.redis import get_redis_client


class RateLimitExceeded(HTTPException):
    """Exception raised when rate limit is exceeded."""

    def __init__(self, retry_after: int = 60):
        super().__init__(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={
                "error": "rate_limit_exceeded",
                "message": "Too many requests. Please try again later.",
                "retry_after": retry_after,
            },
            headers={"Retry-After": str(retry_after)},
        )


class RateLimiter:
    """
    Redis-based rate limiter using sliding window algorithm.

    Supports:
    - Per-IP rate limiting
    - Per-user rate limiting (authenticated)
    - Per-endpoint rate limiting
    - Burst allowance
    """

    def __init__(
        self,
        requests_per_minute: int = 60,
        requests_per_hour: int = 1000,
        burst_size: int = 10,
        key_prefix: str = "ratelimit",
    ):
        self.requests_per_minute = requests_per_minute
        self.requests_per_hour = requests_per_hour
        self.burst_size = burst_size
        self.key_prefix = key_prefix

    def _get_client_ip(self, request: Request) -> str:
        """The address a limit is charged to. See get_client_ip()."""
        from app.core.dependencies import get_client_ip

        return get_client_ip(request)

    def _get_rate_limit_key(
        self,
        request: Request,
        user_id: Optional[str] = None,
        endpoint: Optional[str] = None,
    ) -> str:
        """Generate rate limit key based on context."""
        parts = [self.key_prefix]

        if user_id:
            # Authenticated user - rate limit per user
            parts.append(f"user:{user_id}")
        else:
            # Anonymous - rate limit per IP
            ip = self._get_client_ip(request)
            # Hash IP for privacy
            ip_hash = hashlib.sha256(ip.encode()).hexdigest()[:16]
            parts.append(f"ip:{ip_hash}")

        if endpoint:
            # Endpoint-specific limit
            endpoint_hash = hashlib.md5(endpoint.encode(), usedforsecurity=False).hexdigest()[:8]
            parts.append(f"ep:{endpoint_hash}")

        return ":".join(parts)

    async def check_rate_limit(
        self,
        request: Request,
        user_id: Optional[str] = None,
        endpoint: Optional[str] = None,
        custom_limit: Optional[int] = None,
    ) -> tuple[bool, int, int]:
        """
        Check if request is within rate limits.

        Returns:
            tuple: (is_allowed, remaining_requests, retry_after_seconds)
        """
        redis = get_redis_client()
        if not redis:
            # Redis unavailable - allow request but log warning
            logger.warning("Redis unavailable for rate limiting")
            return True, -1, 0

        key = self._get_rate_limit_key(request, user_id, endpoint)
        limit = custom_limit or self.requests_per_minute
        window = 60  # 1 minute window

        now = datetime.utcnow().timestamp()
        window_start = now - window

        try:
            pipe = redis.pipeline()

            # Remove old entries outside the window
            pipe.zremrangebyscore(key, 0, window_start)

            # Count current requests in window
            pipe.zcard(key)

            # Add current request
            pipe.zadd(key, {str(now): now})

            # Set expiry on the key
            pipe.expire(key, window + 10)

            results = await pipe.execute()
            current_count = results[1]

            if current_count >= limit:
                # Calculate retry after
                oldest = await redis.zrange(key, 0, 0, withscores=True)
                if oldest:
                    retry_after = int(window - (now - oldest[0][1])) + 1
                else:
                    retry_after = window

                return False, 0, retry_after

            remaining = limit - current_count - 1
            return True, remaining, 0

        except Exception as e:
            logger.error(f"Rate limit check failed: {e}")
            # Fail open - allow request on error
            return True, -1, 0

    async def is_allowed(
        self,
        request: Request,
        user_id: Optional[str] = None,
    ) -> bool:
        """Simple check if request is allowed."""
        allowed, _, _ = await self.check_rate_limit(request, user_id)
        return allowed


# Global rate limiter instances
default_limiter = RateLimiter(
    requests_per_minute=60,
    requests_per_hour=1000,
)

auth_limiter = RateLimiter(
    requests_per_minute=10,  # Stricter for auth endpoints
    requests_per_hour=100,
    key_prefix="ratelimit:auth",
)

api_limiter = RateLimiter(
    requests_per_minute=100,
    requests_per_hour=2000,
    key_prefix="ratelimit:api",
)


class RateLimitMiddleware(BaseHTTPMiddleware):
    """
    FastAPI middleware for global rate limiting.

    Applies different limits based on endpoint type:
    - Auth endpoints: 10/min (stricter)
    - API endpoints: 100/min
    - Other: 60/min
    """

    # Endpoints with stricter limits
    # Anything that checks a password: the staff and partner logins (customers
    # have no password). Per IP, so the phone OTP routes are NOT here:
    # carrier-grade NAT puts many real shoppers behind one address. The OTP
    # itself caps wrong guesses per code instead (OTPService.MAX_ATTEMPTS),
    # which is the limit that actually matters.
    AUTH_PATHS = {
        "/api/v1/admin/auth/login",
        "/api/v1/partner/auth/login",
    }

    # Endpoints to skip rate limiting
    SKIP_PATHS = {"/health", "/docs", "/openapi.json", "/redoc"}

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        path = request.url.path

        # Skip rate limiting for certain paths
        if path in self.SKIP_PATHS or path.startswith("/static"):
            return await call_next(request)

        # Select appropriate limiter
        if path in self.AUTH_PATHS:
            limiter = auth_limiter
        elif path.startswith("/api/"):
            limiter = api_limiter
        else:
            limiter = default_limiter

        # Extract user_id if authenticated
        user_id = None
        if hasattr(request.state, "user") and request.state.user:
            user_id = str(request.state.user.id)

        # Check rate limit
        allowed, remaining, retry_after = await limiter.check_rate_limit(
            request, user_id=user_id, endpoint=path
        )

        if not allowed:
            logger.warning(
                f"Rate limit exceeded: {limiter._get_client_ip(request)} on {path}"
            )
            # Returned, never raised. An exception raised inside a
            # BaseHTTPMiddleware bypasses FastAPI's handlers and reaches the
            # server-error middleware, so every throttled request used to go
            # out as a 500 INTERNAL_ERROR -- an attack read as an outage.
            return JSONResponse(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                content={
                    "success": False,
                    "error": {
                        "code": "RATE_LIMIT_EXCEEDED",
                        "message": "Too many requests. Please try again later.",
                        "retry_after": retry_after,
                    },
                },
                headers={"Retry-After": str(retry_after)},
            )

        # Process request
        response = await call_next(request)

        # Add rate limit headers
        if remaining >= 0:
            response.headers["X-RateLimit-Remaining"] = str(remaining)
            response.headers["X-RateLimit-Reset"] = str(int(datetime.utcnow().timestamp()) + 60)

        return response


def rate_limit(
    requests_per_minute: int = 60,
    key_func: Optional[Callable[[Request], str]] = None,
):
    """
    Decorator for endpoint-specific rate limiting.

    Usage:
        @router.get("/expensive-operation")
        @rate_limit(requests_per_minute=5)
        async def expensive_operation():
            ...
    """
    from functools import wraps

    limiter = RateLimiter(
        requests_per_minute=requests_per_minute,
        key_prefix="ratelimit:endpoint",
    )

    def decorator(func: Callable):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            # Find request in args
            request = None
            for arg in args:
                if isinstance(arg, Request):
                    request = arg
                    break

            if not request:
                for value in kwargs.values():
                    if isinstance(value, Request):
                        request = value
                        break

            if request:
                allowed, _, retry_after = await limiter.check_rate_limit(
                    request, endpoint=func.__name__
                )
                if not allowed:
                    raise RateLimitExceeded(retry_after=retry_after)

            return await func(*args, **kwargs)

        return wrapper
    return decorator
