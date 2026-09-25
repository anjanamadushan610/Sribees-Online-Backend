"""
SRIBEESonline — signing in with Google or Apple.

Both go through Firebase Auth. The app signs in with the provider, Firebase
hands it an ID token, and the server verifies that token with the Admin SDK it
already has initialised for push. One verification path for both providers,
and no second copy of Apple's JWKS handling to keep correct as Apple rotates
keys.

**The token is the only thing trusted.** Not the email in the request body,
not the name, not a uid the client claims — those are whatever the client
chose to send. `verify_id_token` checks the signature, the audience and the
expiry against Google's keys, and everything used afterwards comes out of the
verified claims.

**A social sign-in is an account, not an exemption.** The NIC is still
required at the profile gate, still unique, still write-once. Somebody
arriving through Google gets past the phone step and nothing else.
"""
from datetime import datetime

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import User

try:
    from firebase_admin import auth as firebase_auth

    FIREBASE_AUTH_AVAILABLE = True
except ImportError:  # pragma: no cover - mirrors fcm_service's optional import
    FIREBASE_AUTH_AVAILABLE = False


class SocialAuthError(Exception):
    """The token could not be trusted, or the account cannot be used."""


# What Firebase reports, mapped to what we store. Anything else is recorded as
# given rather than rejected: a provider we have not thought about is still a
# verified sign-in, and refusing it would lock out a real person.
_PROVIDERS = {
    "google.com": "google",
    "apple.com": "apple",
}


def _verify(id_token: str) -> dict:
    """The verified claims, or an error. Never trusts the request body."""
    if not FIREBASE_AUTH_AVAILABLE:
        raise SocialAuthError("Social sign-in is not available on this server.")

    from app.services.fcm_service import FCMService

    # Shares the app the push service initialises. Calling it here means a
    # server that has never sent a push still verifies tokens.
    FCMService.initialize()

    try:
        return firebase_auth.verify_id_token(id_token)
    except Exception as exc:
        # Deliberately not echoed to the caller. The reasons -- expired,
        # wrong audience, bad signature -- are useful to us and are a probing
        # aid to anybody else.
        logger.warning(f"Rejected a social sign-in token: {exc}")
        raise SocialAuthError("That sign-in could not be verified.") from exc


async def sign_in(db: AsyncSession, id_token: str) -> tuple[User, bool]:
    """
    Find or create the account behind a verified Google/Apple token.

    Returns ``(user, created)``. The caller issues the session.

    Matching order, and it matters:

      1. ``provider_uid`` — the stable identity. Always tried first.
      2. the verified email, if Firebase says the provider verified it. This
         is what links a Google sign-in to an account someone already made
         with the same address, instead of silently giving them a second one.

    An unverified email is never matched on. Some providers let an address be
    set without proving it, and matching on one would hand over an existing
    account to whoever claimed the address.
    """
    claims = _verify(id_token)

    uid = claims.get("uid") or claims.get("sub")
    if not uid:
        raise SocialAuthError("That sign-in could not be verified.")

    provider_id = (claims.get("firebase", {}) or {}).get("sign_in_provider", "")
    provider = _PROVIDERS.get(provider_id, provider_id or "unknown")
    email = (claims.get("email") or "").strip().lower() or None
    email_verified = bool(claims.get("email_verified"))
    name = (claims.get("name") or "").strip()

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
        await db.commit()
        await db.refresh(existing)
        return existing, False

    user = User(
        # Firebase has already proved the address; requiring our own
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
    db.add(user)
    await db.commit()
    await db.refresh(user)
    logger.info(f"New account from {provider} sign-in: {user.user_id}")
    return user, True


def missing_profile_fields(user: User) -> list[str]:
    """
    What this account still owes before it can order.

    The app reads this to decide whether to send someone to the profile gate.
    Phone and NIC are the two a social sign-in arrives without, and both are
    required to place an order -- the NIC because it is how this business
    recognises a person, the phone because a courier has to ring somebody.
    """
    missing = []
    if not (user.phone or "").strip():
        missing.append("phone")
    if not (user.nic or "").strip():
        missing.append("nic")
    if not (user.full_name or "").strip():
        missing.append("full_name")
    return missing
