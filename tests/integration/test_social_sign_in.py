"""
Google and Apple sign-in. Firebase verifies the token (an external call, the
only thing replaced here: social_auth_service._verify); everything after --
matching the person to an account -- is ours, and is where an account
takeover would happen if it went wrong.
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import func, select


@pytest.fixture
def firebase(monkeypatch):
    """Maps a fake id_token to the claims Firebase would return for it."""
    from app.services import social_auth_service

    tokens: dict[str, dict] = {}

    def _verify(id_token: str) -> dict:
        if id_token not in tokens:
            raise social_auth_service.SocialAuthError("That sign-in could not be verified.")
        return tokens[id_token]

    monkeypatch.setattr(social_auth_service, "_verify", _verify)

    def issue(*, uid=None, email=None, verified=True, provider="google.com", name="ZZ Social") -> str:
        token = f"tok-{uuid.uuid4().hex}{uuid.uuid4().hex}"
        tokens[token] = {"uid": uid or f"uid-{uuid.uuid4().hex}", "email": email, "email_verified": verified,
                         "name": name, "firebase": {"sign_in_provider": provider}}
        return token

    return issue


def _ip() -> dict:
    return {"X-Forwarded-For": f"203.0.113.{uuid.uuid4().int % 250}"}


async def _social(api, token):
    return await api.post("/api/v1/auth/social/login", json={"id_token": token}, headers=_ip())


def _email() -> str:
    return f"zz.social.{uuid.uuid4().hex[:8]}@sribees-ci.lk"


async def test_first_sign_in_creates_one_account_and_later_ones_reuse_it(api, db, world, firebase):
    from app.models.user import User

    uid, email = f"uid-{uuid.uuid4().hex}", _email()
    r = await _social(api, firebase(uid=uid, email=email))
    assert r.status_code == 200, r.text
    r = await _social(api, firebase(uid=uid, email=email, provider="apple.com"))
    assert r.status_code == 200, r.text
    n = (await db.execute(select(func.count()).select_from(User).where(User.provider_uid == uid))).scalar_one()
    assert n == 1


async def test_a_verified_email_links_to_the_existing_account(api, db, world, firebase):
    from app.models.user import User

    email = _email()
    first = await _social(api, firebase(uid=f"uid-{uuid.uuid4().hex}", email=email))
    assert first.status_code == 200
    # Same person, a different provider (a different uid), same verified email.
    r = await _social(api, firebase(uid=f"uid-{uuid.uuid4().hex}", email=email, provider="apple.com"))
    assert r.status_code == 200, r.text
    n = (await db.execute(select(func.count()).select_from(User).where(User.email == email))).scalar_one()
    assert n == 1, "one person, one account"


async def test_an_unverified_email_never_takes_over_an_account(api, db, world, firebase):
    """Some providers let anyone set an address without proving it."""
    from app.models.user import User

    email = _email()
    owner = await _social(api, firebase(uid=f"uid-owner-{uuid.uuid4().hex}", email=email))
    assert owner.status_code == 200
    owner_id = (await db.execute(select(User.user_id).where(User.email == email))).scalar_one()

    r = await _social(api, firebase(uid=f"uid-attacker-{uuid.uuid4().hex}", email=email, verified=False))
    assert r.status_code in (200, 400, 401, 409), r.text
    if r.status_code == 200:
        body = r.json().get("data", r.json())
        user = body.get("user") or {}
        assert str(user.get("user_id") or user.get("userId") or "") != str(owner_id), "account taken over"
    assert r.status_code != 500


async def test_bad_tokens_and_closed_accounts_are_refused(api, db, world, firebase):
    from sqlalchemy import update

    from app.models.user import User

    r = await _social(api, "x" * 40)
    assert r.status_code in (400, 401), r.text

    uid, email = f"uid-{uuid.uuid4().hex}", _email()
    assert (await _social(api, firebase(uid=uid, email=email))).status_code == 200
    await db.execute(update(User).where(User.provider_uid == uid).values(is_active=False))
    await db.commit()
    r = await _social(api, firebase(uid=uid, email=email))
    assert r.status_code in (400, 401, 403), "an inactive account stays out"
