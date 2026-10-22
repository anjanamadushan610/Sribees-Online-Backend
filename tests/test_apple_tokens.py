"""
The client secret Apple's token endpoints take: an ES256 JWT signed with our
Sign in with Apple key. A wrong claim here makes every revoke fail silently
(it is best-effort), so the claims are pinned.
"""
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from app.config.settings import settings
from app.services import apple_tokens


@pytest.fixture
def key(monkeypatch):
    private = ec.generate_private_key(ec.SECP256R1())
    pem = private.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    monkeypatch.setattr(settings, "apple_team_id", "S92FL624YV")
    monkeypatch.setattr(settings, "apple_key_id", "ABC123DEFG")
    # As it arrives from a one-line .env entry.
    monkeypatch.setattr(settings, "apple_private_key", pem.replace("\n", "\n"))
    return private.public_key()


def test_client_secret_is_what_apple_expects(key):
    token = apple_tokens.client_secret("com.sribeesonline.sribeesMobile")

    assert jwt.get_unverified_header(token)["kid"] == "ABC123DEFG"
    claims = jwt.decode(token, key, algorithms=["ES256"], audience="https://appleid.apple.com")
    assert claims["iss"] == "S92FL624YV"
    assert claims["sub"] == "com.sribeesonline.sribeesMobile"
    assert claims["exp"] - claims["iat"] <= 600


@pytest.mark.asyncio
async def test_nothing_is_sent_without_a_key(monkeypatch):
    monkeypatch.setattr(settings, "apple_private_key", "")

    assert await apple_tokens.exchange_code("code", "com.sribeesonline.sribeesMobile") is None
    assert await apple_tokens.revoke("token", "com.sribeesonline.sribeesMobile") is False
