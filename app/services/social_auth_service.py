"""
SRIBEESonline — signing in with Google or Apple.

The app signs in with the provider and sends us the provider's own ID token;
this server verifies it directly against the provider's published signing
keys. No Firebase in between: nobody but us holds a customer's identity.

- **Google**: an RS256 JWT from accounts.google.com whose audience is our Web
  OAuth client (the one the app passes as serverClientId).
- **Apple**: an RS256 JWT from appleid.apple.com whose audience is our Services
  ID (Android's web flow) or, when there is one, our iOS bundle ID.

**The token is the only thing trusted.** Not the email in the request body,
not the name, not a uid the client claims. Signature, issuer, audience and
expiry are checked; everything used afterwards comes out of the verified
claims. When the token carries a nonce, the raw nonce the app sends must hash
to it, so a token lifted from someone else's sign-in cannot be replayed.

**A social sign-in is an account, not an exemption.** The NIC is still
required at the profile gate, still unique, still write-once. Somebody
arriving through Google gets past the phone step and nothing else.
"""
import asyncio
import hashlib
import hmac
from datetime import datetime
from typing import Optional

import jwt
from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import settings
from app.models.user import User
from app.services import apple_tokens


class SocialAuthError(Exception):
    """The token could not be trusted, or the account cannot be used."""


_GOOGLE_ISSUERS = ("accounts.google.com", "https://accounts.google.com")
_APPLE_ISSUER = "https://appleid.apple.com"

# Keys are fetched once and cached; a token signed with a key we have not seen
# makes PyJWKClient refetch, which is how both providers' key rotation works.
_JWKS = {
    "google": jwt.PyJWKClient("https://www.googleapis.com/oauth2/v3/certs", cache_keys=True, lifespan=3600),
    "apple": jwt.PyJWKClient("https://appleid.apple.com/auth/keys", cache_keys=True, lifespan=3600),
}


def _ids(*values: str) -> list[str]:
    out = []
    for value in values:
        out.extend(v.strip() for v in (value or "").split(",") if v.strip())
    return out


def audiences(provider: str) -> list[str]:
    if provider == "google":
        return _ids(settings.google_client_ids)
    if provider == "apple":
        return _ids(settings.apple_services_id, settings.apple_bundle_id)
    return []


def _truthy(value) -> bool:
    # Apple sends email_verified as the STRING "true"; Google as a boolean.
    return value is True or (isinstance(value, str) and value.lower() == "true")


def _decode(provider: str, id_token: str, nonce: Optional[str]) -> dict:
    """Verify one token synchronously (the key fetch is blocking I/O)."""
    audience = audiences(provider)
    if not audience:
        raise SocialAuthError("This sign-in is not available yet.")
    try:
        signing_key = _JWKS[provider].get_signing_key_from_jwt(id_token)
        claims = jwt.decode(
            id_token,
            signing_key.key,
            algorithms=["RS256"],
            audience=audience,
            issuer=_GOOGLE_ISSUERS if provider == "google" else _APPLE_ISSUER,
            options={"require": ["exp", "iat", "iss", "aud", "sub"]},
            leeway=60,
        )
    except Exception as exc:
        # Deliberately not echoed to the caller. The reasons -- expired,
        # wrong audience, bad signature -- are useful to us and are a probing
        # aid to anybody else.
        logger.warning(f"Rejected a {provider} sign-in token: {type(exc).__name__}: {exc}")
        raise SocialAuthError("That sign-in could not be verified.") from exc

    token_nonce = claims.get("nonce")
    if token_nonce is not None or provider == "apple":
        # Apple always gets a nonce from our app; a token without one did not
        # come from our sign-in screen.
        if not nonce or token_nonce is None:
            raise SocialAuthError("That sign-in could not be verified.")
        # Only the HASH is in the token, so only whoever holds the raw value --
        # the app that started this sign-in -- can present it. Accepting the
        # hash itself would let anyone holding the token pass.
        hashed = hashlib.sha256(nonce.encode()).hexdigest()
        if not hmac.compare_digest(hashed, str(token_nonce)):
            logger.warning(f"{provider} sign-in nonce mismatch")
            raise SocialAuthError("That sign-in could not be verified.")
    return claims


async def _verify(provider: str, id_token: str, nonce: Optional[str] = None) -> dict:
    """
    The verified identity, or an error. Never trusts the request body.

    Returns ``{"uid", "email", "email_verified", "name", "aud"}``. ``uid`` is
    namespaced by provider ("google:<sub>", "apple:<sub>"): the two providers'
    subject ids are separate spaces and must never be able to meet.
    """
    if provider not in _JWKS:
        raise SocialAuthError("That sign-in could not be verified.")
    claims = await asyncio.to_thread(_decode, provider, id_token, nonce)
    email = (claims.get("email") or "").strip().lower() or None
    return {
        "uid": f"{provider}:{claims['sub']}",
        "email": email,
        "email_verified": bool(email) and _truthy(claims.get("email_verified")),
        "name": (claims.get("name") or "").strip(),
        # Which of our clients the token was issued to (bundle ID or Services
        # ID): Apple's token endpoints want the same one back.
        "aud": claims["aud"] if isinstance(claims["aud"], str) else claims["aud"][0],
    }


async def sign_in(
    db: AsyncSession,
    provider: str,
    id_token: str,
    *,
    nonce: Optional[str] = None,
    full_name: Optional[str] = None,
    authorization_code: Optional[str] = None,
) -> tuple[User, bool]:
    """
    Find or create the account behind a verified Google/Apple token.

    Returns ``(user, created)``. The caller issues the session.

    Matching order, and it matters:

      1. ``provider_uid`` — the stable identity. Always tried first.
      2. the verified email, if the provider says it verified it. This
         is what links a Google sign-in to an account someone already made
         with the same address, instead of silently giving them a second one.

    An unverified email is never matched on. Some providers let an address be
    set without proving it, and matching on one would hand over an existing
    account to whoever claimed the address.
    """
    identity = await _verify(provider, id_token, nonce)

    uid = identity["uid"]
    email = identity["email"]
    email_verified = identity["email_verified"]
    # Apple's token carries no name; the app passes the one Apple showed it,
    # once. It only ever fills an empty display name.
    name = identity["name"] or (full_name or "").strip()[:200]

    # 1. The stable identity.
    existing = (
        await db.execute(select(User).where(User.provider_uid == uid))
    ).scalar_one_or_none()

    # 2. A verified email they already have an account with.
    if existing is None and email and email_verified:
        existing = (
            await db.execute(
                select(User).where(User.email == email, User.deleted_at.is_(None))
            )
        ).scalar_one_or_none()

    if existing is not None:
        if existing.deleted_at is not None:
            # Closing an account is a decision. Signing in again must not
            # quietly undo it -- they register afresh, and the old row keeps
            # its history against the same NIC.
            raise SocialAuthError(
                "This account has been closed. Please sign up again."
            )
        if not existing.is_active:
            raise SocialAuthError("Your account is inactive.")

        # Bind the uid the first time an existing account signs in socially,
        # so the next sign-in matches on identity rather than on email.
        if existing.provider_uid is None:
            existing.provider_uid = uid
            existing.auth_provider = provider
        # users.last_login is TIMESTAMP WITHOUT TIME ZONE (naive UTC, as
        # auth_service writes it). An aware value is refused by asyncpg, which
        # made every returning Google/Apple sign-in a 500.
        existing.last_login = datetime.utcnow()
        await _keep_apple_token(existing, provider, identity, authorization_code)
        await db.commit()
        await db.refresh(existing)
        return existing, False

    user = User(
        # The provider has already proved the address; requiring our own
        # verification email on top would be asking them to prove it twice.
        # Only an address the provider proved. An unproven one is not the
        # user's to claim -- and when it is already someone else's, storing
        # it hit the unique constraint and failed the sign-in with a 500.
        email=email if email_verified else None,
        full_name=name or "",
        auth_provider=provider,
        provider_uid=uid,
        is_verified=email_verified,
        is_active=True,
        # No phone yet, and no NIC. Both are collected at the profile gate,
        # which every account passes through -- a social sign-in skips the
        # phone STEP, not the phone.
        phone=None,
        password_hash=None,
    )
    await _keep_apple_token(user, provider, identity, authorization_code)
    db.add(user)
    await db.commit()
    await db.refresh(user)
    logger.info(f"New account from {provider} sign-in: {user.user_id}")
    return user, True


async def _keep_apple_token(
    user: User, provider: str, identity: dict, authorization_code: Optional[str]
) -> None:
    """
    Trade an Apple sign-in's authorization code for the refresh token that
    closing the account will revoke. Only after the identity token verified:
    a code is worth nothing without it. A failed exchange keeps whatever the
    account already holds and never fails the sign-in.
    """
    if provider != "apple" or not authorization_code:
        return
    refresh = await apple_tokens.exchange_code(authorization_code, identity["aud"])
    if refresh:
        user.apple_refresh_token = refresh
        user.apple_client_id = identity["aud"]
