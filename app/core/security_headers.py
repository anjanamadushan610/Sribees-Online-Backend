"""
SRIBEESonline - Security Headers Middleware

Adds security-related HTTP headers to all responses.
Implements OWASP security header recommendations.
"""
from typing import Callable

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

from app.config.settings import settings


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """
    Middleware to add security headers to all responses.

    Headers added:
    - X-Content-Type-Options: Prevent MIME type sniffing
    - X-Frame-Options: Prevent clickjacking
    - X-XSS-Protection: Enable XSS filter (legacy browsers)
    - Strict-Transport-Security: Enforce HTTPS
    - Content-Security-Policy: Control resource loading
    - Referrer-Policy: Control referrer information
    - Permissions-Policy: Control browser features
    - Cache-Control: Control caching for sensitive data
    """

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        response = await call_next(request)

        # Prevent MIME type sniffing
        response.headers["X-Content-Type-Options"] = "nosniff"

        # Prevent clickjacking - allow framing from same origin only
        response.headers["X-Frame-Options"] = "SAMEORIGIN"

        # XSS protection for older browsers
        response.headers["X-XSS-Protection"] = "1; mode=block"

        # Only add HSTS in production with HTTPS
        if settings.app_env == "production":
            # Enforce HTTPS for 1 year, include subdomains
            response.headers["Strict-Transport-Security"] = (
                "max-age=31536000; includeSubDomains; preload"
            )

        # Content Security Policy
        # Adjust based on your frontend needs
        csp_directives = [
            "default-src 'self'",
            "script-src 'self' 'unsafe-inline' https://js.stripe.com",
            "style-src 'self' 'unsafe-inline'",
            "img-src 'self' data: https: blob:",
            "font-src 'self' data:",
            "connect-src 'self' https://api.stripe.com wss:",
            "frame-src 'self' https://js.stripe.com",
            "object-src 'none'",
            "base-uri 'self'",
            "form-action 'self'",
        ]
        response.headers["Content-Security-Policy"] = "; ".join(csp_directives)

        # Referrer policy - send origin only for cross-origin requests
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"

        # Permissions policy - disable unnecessary browser features
        permissions = [
            "accelerometer=()",
            "camera=()",
            "geolocation=(self)",  # Allow for delivery location
            "gyroscope=()",
            "magnetometer=()",
            "microphone=()",
            "payment=(self)",  # Allow for payment processing
            "usb=()",
        ]
        response.headers["Permissions-Policy"] = ", ".join(permissions)

        # Cache control for API responses
        if request.url.path.startswith("/api/"):
            # Don't cache authenticated API responses
            if "Authorization" in request.headers:
                response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, private"
                response.headers["Pragma"] = "no-cache"

        return response


class RequestIDMiddleware(BaseHTTPMiddleware):
    """
    Middleware to add request ID for tracing.

    Generates or propagates a unique request ID for each request.
    Useful for debugging and log correlation.
    """

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        import uuid

        # Check for existing request ID (from load balancer or gateway)
        request_id = request.headers.get("X-Request-ID")

        if not request_id:
            request_id = str(uuid.uuid4())

        # Store in request state for use in handlers
        request.state.request_id = request_id

        response = await call_next(request)

        # Add to response headers
        response.headers["X-Request-ID"] = request_id

        return response


class TrustedHostMiddleware(BaseHTTPMiddleware):
    """
    Middleware to validate Host header.

    Prevents host header attacks by only allowing
    requests to configured allowed hosts.
    """

    ALLOWED_HOSTS = {
        "localhost",
        "127.0.0.1",
        "sribeesonline.lk",
        "www.sribeesonline.lk",
        "api.sribeesonline.lk",
        "admin.sribeesonline.lk",
        "api.sribeesonline.com",
        "api.sribees.com",
        "sribees.com",
    }

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        # Skip in development
        if settings.app_env == "development":
            return await call_next(request)

        host = request.headers.get("host", "").split(":")[0].lower()

        if host not in self.ALLOWED_HOSTS:
            from fastapi.responses import JSONResponse
            return JSONResponse(
                status_code=400,
                content={"detail": "Invalid host header"},
            )

        return await call_next(request)


def configure_security_middleware(app):
    """
    Configure all security middleware for the FastAPI app.

    Usage:
        from app.core.security_headers import configure_security_middleware
        configure_security_middleware(app)
    """
    # Order matters - add in reverse order of execution
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(RequestIDMiddleware)

    # Only add trusted host middleware in production
    if settings.app_env == "production":
        app.add_middleware(TrustedHostMiddleware)
