"""
Google and Apple sign-in. Verifying the provider's token needs the provider's
signing keys (an external call, the only thing replaced here:
social_auth_service._verify -- the verification itself is pinned in
tests/security/test_social_tokens.py); everything after -- matching the person
to an account -- is ours, and is where an account takeover would happen if it
went wrong.
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import func, select


@pytest.fixture
def firebase(monkeypatch):
    """Maps a fake id_token to the identity the provider's token would prove.

    (Named for history; there is no Firebase any more.) A token is bound to
    the provider it was issued by, exactly as a real one is.
    """
    from app.services import social_auth_service

    tokens: dict[str, tuple[str, dict]] = {}

    async def _verify(provider: str, id_token: str, nonce=None) -> dict:
        if id_token not in tokens or tokens[id_token][0] != provider:
            raise social_auth_service.SocialAuthError("That sign-in could not be verified.")
        return tokens[id_token][1]

    monkeypatch.setattr(social_auth_service, "_verify", _verify)

    def issue(*, uid=None, email=None, verified=True, provider="google", name="ZZ Social") -> str:
        token = f"tok-{provider}-{uuid.uuid4().hex}{uuid.uuid4().hex}"
        sub = uid or f"uid-{uuid.uuid4().hex}"
        tokens[token] = (provider, {"uid": f"{provider}:{sub}", "email": email,
                                    "email_verified": bool(email) and verified, "name": name})
        return token

    return issue


def _ip() -> dict:
    return {"X-Forwarded-For": f"203.0.113.{uuid.uuid4().int % 250}"}


async def _social(api, token):
    provider = "apple" if token.startswith("tok-apple-") else "google"
    return await api.post(
        "/api/v1/auth/social/login", json={"provider": provider, "id_token": token, "nonce": "n"}, headers=_ip()
    )


def _email() -> str:
    return f"zz.social.{uuid.uuid4().hex[:8]}@sribees-ci.lk"


async def test_first_sign_in_creates_one_account_and_later_ones_reuse_it(api, db, world, firebase):
    from app.models.user import User

    uid, email = f"uid-{uuid.uuid4().hex}", _email()
    r = await _social(api, firebase(uid=uid, email=email))
    assert r.status_code == 200, r.text
    r = await _social(api, firebase(uid=uid, email=email))
    assert r.status_code == 200, r.text
    n = (await db.execute(
        select(func.count()).select_from(User).where(User.provider_uid == f"google:{uid}")
    )).scalar_one()
    assert n == 1


async def test_the_same_subject_at_two_providers_is_two_people(api, db, world, firebase):
    """Google's and Apple's subject ids are separate spaces; they must never meet."""
    from app.models.user import User

    sub = f"uid-{uuid.uuid4().hex}"
    assert (await _social(api, firebase(uid=sub, email=None))).status_code == 200
    assert (await _social(api, firebase(uid=sub, email=None, provider="apple"))).status_code == 200
    rows = (await db.execute(select(User.provider_uid).where(User.provider_uid.like(f"%:{sub}")))).scalars().all()
    assert sorted(rows) == [f"apple:{sub}", f"google:{sub}"]


async def test_a_verified_email_links_to_the_existing_account(api, db, world, firebase):
    from app.models.user import User

    email = _email()
    first = await _social(api, firebase(uid=f"uid-{uuid.uuid4().hex}", email=email))
    assert first.status_code == 200
    # Same person, a different provider (a different uid), same verified email.
    r = await _social(api, firebase(uid=f"uid-{uuid.uuid4().hex}", email=email, provider="apple"))
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
    # A separate account for the claimant, without the unproven address.
    assert r.status_code == 200, r.text
    n = (await db.execute(select(func.count()).select_from(User).where(User.email == email))).scalar_one()
    assert n == 1, "the address still belongs to its owner only"
    assert str(owner_id) not in r.text, "account taken over"


async def test_bad_tokens_and_closed_accounts_are_refused(api, db, world, firebase):
    from sqlalchemy import update

    from app.models.user import User

    r = await _social(api, "x" * 40)
    assert r.status_code == 401, r.text
    r = await api.post("/api/v1/auth/social/login", json={"provider": "facebook", "id_token": "x" * 40})
    assert r.status_code == 422, "only Google and Apple"

    uid, email = f"uid-{uuid.uuid4().hex}", _email()
    assert (await _social(api, firebase(uid=uid, email=email))).status_code == 200
    await db.execute(update(User).where(User.provider_uid == f"google:{uid}").values(is_active=False))
    await db.commit()
    r = await _social(api, firebase(uid=uid, email=email))
    assert r.status_code == 401, "an inactive account stays out"
