"""
Closing an account against the real schema: what is removed, what the order
keeps, and Sign in with Apple being revoked at Apple (App Store 5.1.1(v)).

Apple's token endpoints are the only thing replaced (apple_tokens.exchange_code
and .revoke); identity tokens are faked the same way test_social_sign_in does.
"""
from __future__ import annotations

import json
import uuid

import pytest
from sqlalchemy import func, select


@pytest.fixture
def apple(monkeypatch):
    """Fake Apple: identity tokens, the code exchange and the revoke call."""
    from app.services import apple_tokens, social_auth_service

    tokens: dict[str, dict] = {}
    revoked: list[tuple[str, str]] = []

    async def _verify(provider, id_token, nonce=None):
        if id_token not in tokens:
            raise social_auth_service.SocialAuthError("That sign-in could not be verified.")
        return tokens[id_token]

    async def _exchange(code, client_id):
        return f"refresh-for-{code}"

    async def _revoke(refresh_token, client_id):
        revoked.append((refresh_token, client_id))
        return True

    monkeypatch.setattr(social_auth_service, "_verify", _verify)
    monkeypatch.setattr(apple_tokens, "exchange_code", _exchange)
    monkeypatch.setattr(apple_tokens, "revoke", _revoke)

    def issue(sub: str) -> str:
        token = f"tok-apple-{uuid.uuid4().hex}{uuid.uuid4().hex}"
        tokens[token] = {
            "uid": f"apple:{sub}",
            "email": None,
            "email_verified": False,
            "name": "ZZ Apple",
            "aud": "com.sribeesonline.sribeesMobile",
        }
        return token

    issue.revoked = revoked
    return issue


async def _apple_login(api, token, code=None):
    body = {"provider": "apple", "id_token": token, "nonce": "n"}
    if code:
        body["authorization_code"] = code
    return await api.post(
        "/api/v1/auth/social/login",
        json=body,
        headers={"X-Forwarded-For": f"203.0.113.{uuid.uuid4().int % 250}"},
    )


async def test_closing_removes_personal_data_and_keeps_the_order(
    db, app_redis, make_customer, make_product, place_order
):
    from app.config.redis import RedisKeys
    from app.models.assistant import AssistantProfile
    from app.models.notification import Notification
    from app.models.order import Order
    from app.models.support_ticket import SupportTicket
    from app.models.user import Address, User
    from app.models.wishlist import WishlistItem
    from app.services.account_deletion_service import close_account

    customer = await make_customer()
    product_id = await make_product()
    order = await place_order(customer, product_id)

    spare = uuid.uuid4()
    db.add(
        Address(
            address_id=spare,
            user_id=customer.user_id,
            recipient_name="ZZ Spare",
            phone=customer.phone,
            address_line1="1 Spare Road",
            postal_city="ZZ",
            district="ZZ",
            province="ZZ",
            postal_code="00000",
        )
    )
    db.add(WishlistItem(user_id=customer.user_id, product_id=product_id))
    db.add(AssistantProfile(user_id=customer.user_id, preferences={"favorite_brands": ["ZZ"]}))
    db.add(Notification(user_id=customer.user_id, title="ZZ", message="ZZ"))
    db.add(
        SupportTicket(
            ticket_number=f"ZZ-{uuid.uuid4().hex[:10]}",
            user_id=customer.user_id,
            contact_phone=customer.phone,
            message="ZZ help",
        )
    )
    await db.commit()
    uid = str(customer.user_id)
    await app_redis.set(RedisKeys.chat_history(uid), json.dumps([{"role": "user", "text": "hi"}]))

    await close_account(db, customer.user_id, reason="ZZ test", redis=app_redis)

    db.expire_all()
    user = await db.get(User, customer.user_id)
    assert user.deleted_at is not None and user.is_active is False
    assert user.full_name == "" and user.phone is None and user.email is None

    async def count(model, *where):
        return (await db.execute(select(func.count()).select_from(model).where(*where))).scalar()

    assert await count(Address, Address.address_id == spare) == 0, "a spare address is personal data"
    assert await count(Address, Address.address_id == customer.address_id) == 1, (
        "the address an order went to is part of that order"
    )
    assert await count(WishlistItem, WishlistItem.user_id == customer.user_id) == 0
    assert await count(AssistantProfile, AssistantProfile.user_id == customer.user_id) == 0
    assert await count(Notification, Notification.user_id == customer.user_id) == 0
    assert await count(SupportTicket, SupportTicket.user_id == customer.user_id) == 0
    assert await app_redis.get(RedisKeys.chat_history(uid)) is None

    kept = await db.get(Order, uuid.UUID(order["order_id"]))
    assert kept is not None and kept.user_id == customer.user_id


async def test_apple_is_revoked_and_the_same_apple_id_can_come_back(api, db, apple):
    from app.models.user import User
    from app.services.account_deletion_service import close_account

    sub = f"sub-{uuid.uuid4().hex}"
    r = await _apple_login(api, apple(sub), code="code-1")
    assert r.status_code == 200, r.text

    first = (await db.execute(select(User).where(User.provider_uid == f"apple:{sub}"))).scalar_one()
    assert first.apple_refresh_token == "refresh-for-code-1"
    assert first.apple_client_id == "com.sribeesonline.sribeesMobile"
    first_id = first.user_id

    await close_account(db, first_id)
    assert apple.revoked == [("refresh-for-code-1", "com.sribeesonline.sribeesMobile")]

    db.expire_all()
    closed = await db.get(User, first_id)
    assert closed.provider_uid is None and closed.apple_refresh_token is None

    # Before, the closed row kept the uid and this was refused for good.
    r = await _apple_login(api, apple(sub))
    assert r.status_code == 200, r.text
    again = (await db.execute(select(User).where(User.provider_uid == f"apple:{sub}"))).scalar_one()
    assert again.user_id != first_id, "a fresh account, not the closed one reopened"


async def test_sign_in_without_a_code_still_works(api, db, apple):
    from app.models.user import User

    sub = f"sub-{uuid.uuid4().hex}"
    r = await _apple_login(api, apple(sub))
    assert r.status_code == 200, r.text
    user = (await db.execute(select(User).where(User.provider_uid == f"apple:{sub}"))).scalar_one()
    assert user.apple_refresh_token is None
