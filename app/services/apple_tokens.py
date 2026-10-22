"""
SRIBEESonline — talking to Apple on a customer's behalf.

Verifying an Apple identity token needs nothing secret (see
``social_auth_service``). Two things do:

- **At sign-in** the app also hands over Apple's one-time authorization code.
  We trade it for a refresh token and keep that against the account. It is
  never used to sign anyone in; it exists so the next step is possible.
- **When the account is closed** we revoke that refresh token. App Store
  guideline 5.1.1(v) requires it: deleting an account that signed in with
  Apple must also end the app's authorization at Apple, so the person sees
  Sribees gone from "Apps Using Apple ID".

Both calls authenticate with a client secret: a short-lived ES256 JWT signed
with our Sign in with Apple key (.p8). The ``client_id`` must be the one the
token was issued to -- the bundle ID on iOS, the Services ID on Android's web
flow -- so the account keeps whichever its identity token named.

Best-effort by design. Apple being slow or down must neither fail a sign-in
nor stop an account from closing; both log and carry on.
"""
import time
from typing import Optional

import httpx
import jwt
from loguru import logger

from app.config.settings import settings

_APPLE = "https://appleid.apple.com"
REQUEST_TIMEOUT = 10.0


def configured() -> bool:
    return bool(settings.apple_team_id and settings.apple_key_id and settings.apple_private_key)


def _private_key() -> str:
    # A .env holds one line per key, so the .p8 usually arrives with its
    # newlines escaped.
    return settings.apple_private_key.replace("\\n", "\n").strip()


def client_secret(client_id: str) -> str:
    now = int(time.time())
    return jwt.encode(
        {
            "iss": settings.apple_team_id,
            "iat": now,
            "exp": now + 300,
            "aud": _APPLE,
            "sub": client_id,
        },
        _private_key(),
        algorithm="ES256",
        headers={"kid": settings.apple_key_id},
    )


async def exchange_code(code: str, client_id: str) -> Optional[str]:
    """The refresh token behind a sign-in's authorization code, or None."""
    if not configured() or not code or not client_id:
        return None
    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
            r = await client.post(
                f"{_APPLE}/auth/token",
                data={
                    "client_id": client_id,
                    "client_secret": client_secret(client_id),
                    "code": code,
                    "grant_type": "authorization_code",
                },
            )
        if r.status_code != 200:
            # Apple's error body is a short code ("invalid_grant"), no token.
            logger.warning(f"Apple code exchange refused: {r.status_code} {r.text[:200]}")
            return None
        return r.json().get("refresh_token") or None
    except Exception as exc:
        logger.warning(f"Apple code exchange failed: {type(exc).__name__}: {exc}")
        return None


async def revoke(refresh_token: str, client_id: str) -> bool:
    """End the app's authorization at Apple. True when Apple accepted it."""
    if not configured() or not refresh_token or not client_id:
        return False
    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
            r = await client.post(
                f"{_APPLE}/auth/revoke",
                data={
                    "client_id": client_id,
                    "client_secret": client_secret(client_id),
                    "token": refresh_token,
                    "token_type_hint": "refresh_token",
                },
            )
        if r.status_code != 200:
            logger.warning(f"Apple revoke refused: {r.status_code} {r.text[:200]}")
            return False
        return True
    except Exception as exc:
        logger.warning(f"Apple revoke failed: {type(exc).__name__}: {exc}")
        return False
