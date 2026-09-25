"""
Token forgery and misuse.

Each case here is a real attack on a JWT API, sent to one route per realm. The
answer must always be 401 and must never reach the database -- the stubbed
session in conftest turns any query into a 500, which fails these tests.
"""
from __future__ import annotations

import base64
import json
from datetime import timedelta

import pytest

from app.core import security
from tests.security.conftest import make_token

pytestmark = pytest.mark.security

# One route per realm, all GETs with no parameters, so a failure points at the
# token handling rather than at request validation.
CUSTOMER_ROUTE = "/api/v1/cart"
ADMIN_ROUTE = "/api/v1/admin/dashboard/stats"
PARTNER_ROUTE = "/api/v1/partner/me"


def _b64(obj: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()


def _alg_none_token(**claims) -> str:
    header = _b64({"alg": "none", "typ": "JWT"})
    body = _b64({"sub": "00000000-0000-0000-0000-000000000001", "type": "access", **claims})
    return f"{header}.{body}."


FORGERIES = {
    "wrong_signing_key": lambda realm: make_token(key="attacker-chosen-secret", **realm),
    "expired": lambda realm: make_token(expires_in=timedelta(seconds=-5), **realm),
    "refresh_token_used_as_access": lambda realm: make_token(token_type="refresh", **realm),
    "alg_none": lambda realm: _alg_none_token(**{k: True for k, v in realm.items() if v}),
    "garbage": lambda realm: "not.a.jwt",
    "empty_signature": lambda realm: make_token(**realm).rsplit(".", 1)[0] + ".",
    "missing_type_claim": lambda realm: make_token(token_type="", **realm),
}

REALMS = {
    CUSTOMER_ROUTE: {},
    ADMIN_ROUTE: {"is_admin": True},
    PARTNER_ROUTE: {"is_partner": True},
}


@pytest.mark.parametrize("route", list(REALMS))
@pytest.mark.parametrize("forgery", list(FORGERIES))
async def test_forged_tokens_are_refused(client, route, forgery):
    token = FORGERIES[forgery](REALMS[route])
    r = await client.get(route, headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 401, f"{forgery} on {route} -> {r.status_code}: {r.text[:200]}"


@pytest.mark.parametrize("route", list(REALMS))
@pytest.mark.parametrize(
    "header",
    ["Bearer", "Basic dXNlcjpwYXNz", "bearer  ", "Token abc", "Bearer a b"],
)
async def test_malformed_authorization_headers_are_refused(client, route, header):
    r = await client.get(route, headers={"Authorization": header})
    assert r.status_code in {401, 403}, f"{header!r} on {route} -> {r.status_code}"


async def test_unauthenticated_errors_do_not_leak_internals(client):
    r = await client.get(ADMIN_ROUTE, headers={"Authorization": "Bearer not.a.jwt"})
    body = r.text.lower()
    for leak in ("traceback", "jose", "sqlalchemy", "secret", 'file "'):
        assert leak not in body, f"401 body mentions {leak!r}: {r.text[:300]}"


# ---------------------------------------------------------------------------
# app.core.security, directly
# ---------------------------------------------------------------------------


def test_password_hashes_are_argon2_salted_and_verifiable():
    a = security.hash_password("Correct-Horse-9")
    b = security.hash_password("Correct-Horse-9")
    assert a.startswith("$argon2")
    assert a != b, "same password must hash differently (salt)"
    assert security.verify_password("Correct-Horse-9", a)
    assert not security.verify_password("correct-horse-9", a)
    assert not security.verify_password("Correct-Horse-9", "not-a-hash")


def test_token_pair_types_do_not_cross():
    pair = security.create_token_pair("u-1", "a@b.test", "s-1")
    assert security.verify_token(pair["access_token"], "access")["sub"] == "u-1"
    assert security.verify_token(pair["refresh_token"], "refresh")["sub"] == "u-1"
    assert security.verify_token(pair["access_token"], "refresh") is None
    assert security.verify_token(pair["refresh_token"], "access") is None


def test_admin_claims_only_appear_on_admin_pairs():
    customer = security.decode_token(security.create_token_pair("u", "e", "s")["access_token"])
    admin = security.decode_token(
        security.create_token_pair("a", "e", "s", is_admin=True, role="branch_manager")["access_token"]
    )
    assert "is_admin" not in customer and "role" not in customer
    assert admin["is_admin"] is True and admin["role"] == "branch_manager"


def test_access_tokens_are_short_lived_and_remember_me_extends_refresh_only():
    pair = security.create_token_pair("u", "e", "s", remember_me=True)
    access = security.decode_token(pair["access_token"])
    refresh = security.decode_token(pair["refresh_token"])
    assert access["exp"] - access["iat"] <= 60 * 60, "access token must not outlive an hour"
    assert refresh["exp"] - refresh["iat"] >= 29 * 24 * 3600


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("Bearer abc", "abc"),
        ("bearer abc", "abc"),
        ("Bearer", None),
        ("Basic abc", None),
        ("Bearer a b", None),
        ("", None),
    ],
)
def test_extract_token_from_header(header, expected):
    assert security.extract_token_from_header(header) == expected
