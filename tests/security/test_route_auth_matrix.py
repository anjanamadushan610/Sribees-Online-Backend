"""
Authentication matrix: every route, every realm.

The API has three identity stores -- customers, admins, and the professional
referral partners -- and ~320 routes. Reviewing each route by hand for "does it
check who is asking?" does not scale and does not stay true, so this file
derives the answer from the dependency graph and then proves it over HTTP:

1. The set of routes that need NO identity is pinned. Adding a public route
   fails CI until someone adds it to PUBLIC_ROUTES below, i.e. until a human
   has decided it should be public.
2. Every other route answers an anonymous request with 401/403 -- never data,
   never a 500.
3. A token from one realm never opens a route of another (customer -> admin,
   partner -> admin, admin -> customer, and so on).

None of this needs a database: identity is checked before storage is read.
"""
from __future__ import annotations

import pytest

from tests.security.conftest import concrete, make_token, route_cases

pytestmark = pytest.mark.security

# Reviewed on 2026-09-25. Each of these is intentionally reachable without a
# login: the storefront (browse before sign-in), sign-in itself, signed
# webhooks, and the static app-config the app reads on its splash screen.
PUBLIC_ROUTES: set[tuple[str, str]] = {
    ("GET", "/health"),
    ("GET", "/api"),
    ("GET", "/.well-known/assetlinks.json"),
    ("GET", "/p/{product_id}"),
    # --- sign-in / sign-up ---------------------------------------------------
    # Customers: phone OTP, Google or Apple only; no email/password (2026-09-25).
    ("POST", "/api/v1/auth/phone/request-otp"),
    ("POST", "/api/v1/auth/phone/verify-otp"),
    ("POST", "/api/v1/auth/refresh-token"),
    ("POST", "/api/v1/auth/refresh"),
    ("POST", "/api/v1/auth/social/login"),
    ("POST", "/api/v1/admin/auth/login"),
    ("POST", "/api/v1/admin/auth/refresh"),
    ("POST", "/api/v1/partner/auth/login"),
    # --- signed or static callbacks -----------------------------------------
    ("POST", "/api/v1/payments/webhook"),  # Stripe signature, see test_webhooks
    ("GET", "/api/v1/payments/marx/return"),  # static "return to the app" page
    ("POST", "/api/v1/courier/webhooks/shipment-status"),  # HMAC, see test_webhooks
    # --- app bootstrap -------------------------------------------------------
    ("GET", "/api/v1/app/splash-config"),
    ("GET", "/api/v1/app/support-info"),
    ("GET", "/api/v1/app/app-config"),
    # The app's presence heartbeat: guests have the app open too. Writes only
    # its own install's row; a customer comes from the token, never the body.
    ("POST", "/api/v1/app/usage/heartbeat"),
    # --- storefront (guest browsing; a login only personalises) --------------
    ("GET", "/api/v1/categories"),
    ("GET", "/api/v1/categories/{category_id}"),
    ("GET", "/api/v1/products"),
    ("GET", "/api/v1/products/featured"),
    ("GET", "/api/v1/products/home-feed"),
    ("GET", "/api/v1/products/search"),
    ("GET", "/api/v1/products/category/{category_id}"),
    ("GET", "/api/v1/products/{product_id}"),
    ("GET", "/api/v1/products/{product_id}/variants"),
    ("GET", "/api/v1/products/{product_id}/reviews"),
    ("GET", "/api/v1/coupons/available"),
    ("GET", "/api/v1/coupons/collected"),
    # The offers a guest may browse, as /coupons/available shows them. Pricing
    # an order (`items` / `use_cart`) is a 401 without a login.
    ("POST", "/api/v1/coupons/evaluate"),
    ("GET", "/api/v1/banners"),
    ("POST", "/api/v1/banners/events"),
    ("POST", "/api/v1/branch/resolve-by-location"),
    # A live branch's name and district, for "Shared from the Kandy branch" on
    # a product link a guest may open. Nothing the storefront doesn't show.
    ("GET", "/api/v1/branch/public/{branch_id}"),
    ("GET", "/api/v1/delivery/slots"),
    ("GET", "/api/v1/locations/provinces"),
    ("GET", "/api/v1/locations/districts"),
    ("GET", "/api/v1/locations/post-offices"),
    ("GET", "/api/v1/locations/postal-cities"),
    # --- guest-capable, keyed by X-Device-Id ---------------------------------
    ("POST", "/api/v1/chat/message"),
    ("POST", "/api/v1/chat/message/stream"),
    ("GET", "/api/v1/chat/history"),
    ("DELETE", "/api/v1/chat/history"),
    ("POST", "/api/v1/support/tickets"),
    ("GET", "/api/v1/support/my-tickets"),  # see test_support_ticket_lookup
}

ADMIN = route_cases("admin")
CUSTOMER = route_cases("customer")
PARTNER = route_cases("partner")
PUBLIC = route_cases("public")

DENIED = {401, 403}


def _ids(cases):
    return [f"{m} {p}" for m, p in cases]


def test_the_public_surface_is_exactly_the_reviewed_list():
    actual = set(PUBLIC)
    unreviewed = sorted(actual - PUBLIC_ROUTES)
    gone = sorted(PUBLIC_ROUTES - actual)
    assert not unreviewed, (
        "These routes accept requests with NO login. If that is intended, add "
        f"them to PUBLIC_ROUTES with a reason; otherwise add an auth dependency: {unreviewed}"
    )
    assert not gone, f"Listed as public but no longer exist or now require a login: {gone}"


def test_every_realm_is_populated():
    # A refactor that renames get_current_admin would silently empty ADMIN and
    # turn the 170 checks below into zero checks. Fail loudly instead.
    assert len(ADMIN) > 100
    assert len(CUSTOMER) > 50
    assert len(PARTNER) >= 3


async def _call(client, method: str, path: str, token: str | None = None):
    headers = {"X-Device-Id": "security-suite"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    kwargs = {"headers": headers}
    if method in {"POST", "PUT", "PATCH"}:
        kwargs["json"] = {}
    return await client.request(method, concrete(path), **kwargs)


@pytest.mark.parametrize(("method", "path"), ADMIN + CUSTOMER + PARTNER, ids=_ids(ADMIN + CUSTOMER + PARTNER))
async def test_anonymous_requests_are_refused(client, method, path):
    r = await _call(client, method, path)
    assert r.status_code in DENIED, f"{method} {path} -> {r.status_code}: {r.text[:300]}"


@pytest.mark.parametrize(("method", "path"), ADMIN, ids=_ids(ADMIN))
async def test_customer_and_partner_tokens_never_open_admin_routes(client, method, path):
    for token in (make_token(), make_token(is_partner=True)):
        r = await _call(client, method, path, token)
        assert r.status_code in DENIED, f"{method} {path} -> {r.status_code}: {r.text[:300]}"


@pytest.mark.parametrize(("method", "path"), CUSTOMER, ids=_ids(CUSTOMER))
async def test_admin_and_partner_tokens_never_open_customer_routes(client, method, path):
    for token in (make_token(is_admin=True), make_token(is_partner=True)):
        r = await _call(client, method, path, token)
        assert r.status_code in DENIED, f"{method} {path} -> {r.status_code}: {r.text[:300]}"


@pytest.mark.parametrize(("method", "path"), PARTNER, ids=_ids(PARTNER))
async def test_admin_and_customer_tokens_never_open_partner_routes(client, method, path):
    for token in (make_token(is_admin=True), make_token()):
        r = await _call(client, method, path, token)
        assert r.status_code in DENIED, f"{method} {path} -> {r.status_code}: {r.text[:300]}"
