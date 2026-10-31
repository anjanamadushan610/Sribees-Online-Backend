"""
HTTP-layer hardening: headers, CORS, error bodies and rate limiting.

These are the properties a scanner (or an attacker) checks first. They live in
middleware, so a refactor of main.py can drop one without any feature test
noticing -- which is why each gets an explicit assertion here.
"""
from __future__ import annotations

import pytest

from app.config.settings import settings

pytestmark = pytest.mark.security


async def test_security_headers_are_on_every_response(client):
    for path in ("/health", "/api/v1/cart", "/does-not-exist"):
        r = await client.get(path)
        h = r.headers
        assert h["X-Content-Type-Options"] == "nosniff", path
        assert h["X-Frame-Options"] in {"DENY", "SAMEORIGIN"}, path
        assert "default-src" in h["Content-Security-Policy"], path
        assert "object-src 'none'" in h["Content-Security-Policy"], path
        assert h["Referrer-Policy"] == "strict-origin-when-cross-origin", path
        assert "camera=()" in h["Permissions-Policy"], path
        assert h.get("X-Request-ID"), path


async def test_authenticated_api_responses_are_not_cacheable(client):
    r = await client.get("/api/v1/cart", headers={"Authorization": "Bearer x"})
    assert "no-store" in r.headers.get("Cache-Control", "")


async def test_request_id_is_echoed_for_log_correlation(client):
    r = await client.get("/health", headers={"X-Request-ID": "trace-abc-123"})
    assert r.headers["X-Request-ID"] == "trace-abc-123"


async def test_health_answers_without_storage(client):
    r = await client.get("/health")
    assert r.status_code == 200
    assert r.json()["success"] is True


# ---------------------------------------------------------------------------
# CORS
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "origin",
    [
        "https://evil.example.com",
        "https://sribees.com.evil.example",
        "http://api.sribees.com",  # plain http must not match the https rule
        "https://evilsribees.com",
        "https://evilsribeesonline.com",
        "https://sribeesonline.com.evil.example",
        "https://shop.sribeesonline.com",
        "http://sribeesonline.com",
        # Only the development server takes Netlify previews, and only because
        # its environment says so (CORS_ORIGIN_REGEX).
        "https://some-preview.netlify.app",
        "null",
    ],
)
async def test_cors_does_not_reflect_untrusted_origins(client, origin):
    r = await client.options(
        "/api/v1/products",
        headers={"Origin": origin, "Access-Control-Request-Method": "GET"},
    )
    assert r.headers.get("access-control-allow-origin") not in {origin, "*"}, origin


@pytest.mark.parametrize(
    "origin",
    [
        "https://admin.sribees.com",
        "https://sribees.com",
        "https://sribeesonline.com",
        "https://www.sribeesonline.com",
    ],
)
async def test_cors_allows_our_own_origins(client, origin):
    r = await client.options(
        "/api/v1/products",
        headers={"Origin": origin, "Access-Control-Request-Method": "GET"},
    )
    assert r.headers.get("access-control-allow-origin") == origin


def test_cors_pattern_comes_from_settings(storage_free_app):
    """The environment decides which origins match, so development can be
    wider than production without a second copy of the rule in main.py."""
    from starlette.middleware.cors import CORSMiddleware

    cors = next(m for m in storage_free_app.user_middleware if m.cls is CORSMiddleware)
    assert cors.kwargs["allow_origin_regex"] == settings.cors_origin_regex


# ---------------------------------------------------------------------------
# Error bodies
# ---------------------------------------------------------------------------


async def test_unhandled_errors_hide_internals_outside_debug(storage_free_app, client, monkeypatch):
    monkeypatch.setattr(settings, "debug", False)

    async def boom():
        raise RuntimeError("password=hunter2 at /app/secret.py line 7")

    storage_free_app.add_api_route("/__test_boom", boom, methods=["GET"])
    try:
        r = await client.get("/__test_boom")
    finally:
        storage_free_app.router.routes = [
            rt for rt in storage_free_app.router.routes if getattr(rt, "path", "") != "/__test_boom"
        ]
    assert r.status_code == 500
    assert r.json()["error"]["code"] == "INTERNAL_ERROR"
    assert "hunter2" not in r.text and "secret.py" not in r.text


async def test_validation_errors_are_422_not_500(client):
    r = await client.post("/api/v1/auth/phone/request-otp", json={"phone": "not-a-phone"})
    assert r.status_code in {400, 422}


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------


async def test_login_is_rate_limited_with_a_429(client):
    """
    Credential stuffing protection. The limiter must answer with a real 429
    and a Retry-After -- it used to raise inside the middleware, which reached
    the client as a 500 INTERNAL_ERROR: an attack read as an outage.
    """
    codes = []
    for _ in range(14):
        r = await client.post(
            "/api/v1/admin/auth/login",
            json={"email": "victim@example.test", "password": "guess"},
            headers={"X-Forwarded-For": "203.0.113.9"},
        )
        codes.append(r.status_code)
        if r.status_code == 429:
            assert r.headers.get("Retry-After")
            assert r.json()["error"]["code"] == "RATE_LIMIT_EXCEEDED"
    assert 500 not in codes, codes
    assert 429 in codes, codes
    assert codes.index(429) <= 10, f"auth limit is 10/min: {codes}"


async def test_a_forged_forwarded_for_does_not_reset_the_limit(client):
    """
    X-Forwarded-For is client-writable; only the right-most hop was added by
    our proxy. Rotating a fake left-hand address per request used to give
    every request a fresh budget.
    """
    codes = []
    for i in range(14):
        r = await client.post(
            "/api/v1/admin/auth/login",
            json={"email": "admin@example.test", "password": "guess"},
            headers={"X-Forwarded-For": f"10.0.0.{i}, 198.51.100.7"},
        )
        codes.append(r.status_code)
    assert 429 in codes, codes


def test_client_ip_is_the_hop_our_proxy_appended():
    from starlette.requests import Request

    from app.core.dependencies import get_client_ip

    def req(xff):
        headers = [(b"x-forwarded-for", xff.encode())] if xff is not None else []
        return Request({"type": "http", "headers": headers, "client": ("172.18.0.1", 5000)})

    assert get_client_ip(req("6.6.6.6, 198.51.100.7")) == "198.51.100.7"
    assert get_client_ip(req("198.51.100.7")) == "198.51.100.7"
    assert get_client_ip(req(" , ")) == "172.18.0.1"
    assert get_client_ip(req(None)) == "172.18.0.1"


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/admin/auth/login",
        "/api/v1/partner/auth/login",
    ],
)
def test_every_password_endpoint_gets_the_strict_limit(path):
    """
    Anything that checks a password (staff and partners; customers have
    none) sits on the 10/min limiter, not the 100/min general one. (OTP routes are protected per code
    instead -- see test_otp.py -- because mobile carriers NAT many shoppers
    behind one address.)
    """
    from app.core.rate_limiter import RateLimitMiddleware

    assert path in RateLimitMiddleware.AUTH_PATHS
