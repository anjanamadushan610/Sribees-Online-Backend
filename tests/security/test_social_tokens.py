"""
Google and Apple ID tokens, verified here without Firebase.

A forged or misdirected token is an account takeover, so these pin every check
a real token passes: signature, algorithm, issuer, audience, expiry, and for
Apple the nonce that binds the token to the sign-in our app started. The
providers' key sets are replaced with a local RSA key; everything else is the
production code path.
"""
from __future__ import annotations

import hashlib
import time
from types import SimpleNamespace

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from app.config.settings import settings
from app.services import social_auth_service as social
from app.services.social_auth_service import SocialAuthError

pytestmark = pytest.mark.security

WEB_CLIENT = "123-web.apps.googleusercontent.com"
SERVICES_ID = "com.sribees.online.webauth"

KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


class _Keys:
    def get_signing_key_from_jwt(self, token):
        return SimpleNamespace(key=KEY.public_key())


@pytest.fixture(autouse=True)
def configured(monkeypatch):
    monkeypatch.setattr(settings, "google_client_ids", WEB_CLIENT)
    monkeypatch.setattr(settings, "apple_services_id", SERVICES_ID)
    monkeypatch.setattr(settings, "apple_bundle_id", "")
    monkeypatch.setitem(social._JWKS, "google", _Keys())
    monkeypatch.setitem(social._JWKS, "apple", _Keys())


def _token(provider="google", key=KEY, alg="RS256", **over) -> str:
    now = int(time.time())
    claims = {
        "iss": "https://accounts.google.com" if provider == "google" else "https://appleid.apple.com",
        "aud": WEB_CLIENT if provider == "google" else SERVICES_ID,
        "sub": "1098765",
        "iat": now,
        "exp": now + 600,
        "email": "Nimal@Example.com",
        "email_verified": True if provider == "google" else "true",
    }
    if provider == "google":
        claims["name"] = "Nimal Perera"
    claims.update(over)
    claims = {k: v for k, v in claims.items() if v is not None}
    return jwt.encode(claims, key, algorithm=alg)


def _hashed(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


async def test_a_real_google_token_proves_who_it_is():
    who = await social._verify("google", _token())
    assert who == {
        "uid": "google:1098765",
        "email": "nimal@example.com",
        "email_verified": True,
        "name": "Nimal Perera",
    }


async def test_a_real_apple_token_with_its_nonce():
    who = await social._verify("apple", _token("apple", nonce=_hashed("raw-1")), nonce="raw-1")
    assert who["uid"] == "apple:1098765"
    assert who["email_verified"] is True, "Apple says \"true\" as a string"


@pytest.mark.parametrize(
    "token, why",
    [
        (lambda: _token(key=OTHER_KEY), "signed by someone else"),
        (lambda: _token(aud="somebody-elses-app.apps.googleusercontent.com"), "issued to another app"),
        (lambda: _token(iss="https://evil.example"), "wrong issuer"),
        (lambda: _token(exp=int(time.time()) - 3600, iat=int(time.time()) - 7200), "expired"),
        (lambda: _token(sub=None), "no subject"),
        (lambda: jwt.encode({"sub": "1", "aud": WEB_CLIENT, "iss": "accounts.google.com"}, "secret", algorithm="HS256"),
         "HMAC instead of RSA"),
        (lambda: "not.a.token", "rubbish"),
    ],
)
async def test_a_google_token_that_should_not_pass(token, why):
    with pytest.raises(SocialAuthError):
        await social._verify("google", token())


async def test_a_google_token_is_not_an_apple_token():
    with pytest.raises(SocialAuthError):
        await social._verify("apple", _token("google"), nonce="x")


@pytest.mark.parametrize(
    "token_nonce, sent, why",
    [
        (None, "raw-1", "token without a nonce: not from our sign-in screen"),
        (_hashed("raw-1"), None, "app sent no nonce"),
        (_hashed("raw-1"), "raw-2", "someone else's sign-in"),
        (_hashed("raw-1"), _hashed("raw-1"), "the hash copied out of the token is not the raw value"),
    ],
)
async def test_apple_nonce(token_nonce, sent, why):
    with pytest.raises(SocialAuthError):
        await social._verify("apple", _token("apple", nonce=token_nonce), nonce=sent)


async def test_a_google_nonce_is_checked_when_present():
    token = _token(nonce=_hashed("raw-g"))
    assert (await social._verify("google", token, nonce="raw-g"))["uid"] == "google:1098765"
    with pytest.raises(SocialAuthError):
        await social._verify("google", token, nonce="other")


async def test_an_unverified_email_is_reported_as_such():
    who = await social._verify("google", _token(email_verified=False))
    assert who["email_verified"] is False


async def test_nothing_is_accepted_until_the_provider_is_configured(monkeypatch):
    monkeypatch.setattr(settings, "google_client_ids", "")
    with pytest.raises(SocialAuthError, match="not available"):
        await social._verify("google", _token())


async def test_unknown_provider():
    with pytest.raises(SocialAuthError):
        await social._verify("facebook", _token())


# ---------------------------------------------------------------------------
# Over HTTP
# ---------------------------------------------------------------------------


async def test_apple_android_callback_hands_back_only_apples_fields(client, monkeypatch):
    monkeypatch.setattr(settings, "android_package", "com.sribeesonline.sribees_mobile")
    r = await client.post(
        "/api/v1/auth/apple/callback",
        data={"code": "c1", "id_token": "t1", "state": "s1", "evil": "x", "user": "{}"},
        follow_redirects=False,
    )
    assert r.status_code == 307
    location = r.headers["location"]
    assert location.startswith("intent://callback?")
    assert location.endswith("#Intent;package=com.sribeesonline.sribees_mobile;scheme=signinwithapple;end")
    assert "code=c1" in location and "id_token=t1" in location and "state=s1" in location
    assert "evil" not in location


async def test_app_config_tells_the_app_how_to_start_a_sign_in(client, monkeypatch):
    monkeypatch.setattr(settings, "google_client_ids", f"{WEB_CLIENT},second.apps.googleusercontent.com")
    from app.config import redis as redis_module

    await redis_module._redis_client.delete("app:mobile_config")
    r = await client.get("/api/v1/app/app-config", headers={"Host": "devapionline.sribees.com"})
    sign_in = r.json()["data"]["sign_in"]
    assert sign_in == {
        "google_server_client_id": WEB_CLIENT,
        "apple_services_id": SERVICES_ID,
        "apple_redirect_uri": "https://devapionline.sribees.com/api/v1/auth/apple/callback",
    }
    r = await client.get("/api/v1/app/app-config", headers={"Host": "evil.example"})
    assert r.json()["data"]["sign_in"]["apple_redirect_uri"].startswith("https://api.sribees.com/")
