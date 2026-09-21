"""
The customer's identity: the NIC that names them, and the two numbers a
courier can reach them on.

These cover the rules that are easy to regress because they live in three
different layers -- format normalisation, the write-once guard in the
service, and the "two different numbers" rule on the order schema.
"""

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from app.core.exceptions import DuplicateError, ValidationError
from app.models.user import User
from app.schemas.auth import UpdateProfileRequest
from app.schemas.order import CreateOrderRequest
from app.services.auth_service import AuthService
from app.utils.identity import normalize_mobile, normalize_nic


def _result(value):
    row = MagicMock()
    row.scalar_one_or_none.return_value = value
    return row


def _account(**kwargs) -> User:
    user = User(user_id=uuid4(), full_name="Test Customer", **kwargs)
    user.is_verified = True
    user.is_phone_verified = True
    user.two_factor_enabled = False
    user.created_at = datetime.now(timezone.utc)
    user.updated_at = datetime.now(timezone.utc)
    user.last_login = None
    return user


@pytest.mark.parametrize(
    "typed,stored",
    [
        ("123456789v", "123456789V"),
        ("123456789X", "123456789X"),
        ("19 9512 345678", "199512345678"),
        ("95-1234-5678-90", "951234567890"),
    ],
)
def test_nic_is_stored_in_one_canonical_shape(typed, stored):
    # A UNIQUE column is only unique if "123456789v" and "123456789V" are the
    # same string by the time they reach it.
    assert normalize_nic(typed) == stored


@pytest.mark.parametrize(
    "bad", ["12345678V", "1234567890", "123456789Y", "abcdefghi", "", "1234567890123"]
)
def test_nic_rejects_anything_that_is_not_a_nic(bad):
    with pytest.raises(ValueError):
        normalize_nic(bad)


@pytest.mark.parametrize(
    "typed", ["0771234567", "771234567", "+94771234567", "94 77 123 4567"]
)
def test_every_way_of_typing_a_mobile_lands_on_the_same_number(typed):
    assert normalize_mobile(typed) == "+94771234567"


@pytest.mark.parametrize("bad", ["0112345678", "07712345", "0771234567890", ""])
def test_mobile_rejects_landlines_and_wrong_lengths(bad):
    with pytest.raises(ValueError):
        normalize_mobile(bad)


@pytest.mark.asyncio
async def test_a_new_account_can_set_its_nic_once():
    user = _account(nic=None)
    db = AsyncMock()
    db.execute.side_effect = [_result(user), _result(None)]
    response = await AuthService.update_profile(
        SimpleNamespace(user_id=user.user_id),
        UpdateProfileRequest(nic="123456789v"),
        db,
    )
    assert user.nic == "123456789V"
    assert response.user.nic == "123456789V"


@pytest.mark.asyncio
async def test_an_account_cannot_rewrite_its_own_nic():
    """Identity a customer can edit is not identity. Support does this."""
    user = _account(nic="199512345678")
    db = AsyncMock()
    db.execute.side_effect = [_result(user)]
    with pytest.raises(ValidationError):
        await AuthService.update_profile(
            SimpleNamespace(user_id=user.user_id),
            UpdateProfileRequest(nic="123456789V"),
            db,
        )
    assert user.nic == "199512345678"


@pytest.mark.asyncio
async def test_two_accounts_cannot_claim_the_same_nic():
    user = _account(nic=None)
    other = _account(nic="123456789V")
    db = AsyncMock()
    db.execute.side_effect = [_result(user), _result(other)]
    with pytest.raises(DuplicateError):
        await AuthService.update_profile(
            SimpleNamespace(user_id=user.user_id),
            UpdateProfileRequest(nic="123456789V"),
            db,
        )


@pytest.mark.asyncio
async def test_the_second_number_cannot_be_the_first_one_again():
    user = _account(nic="199512345678")
    user.phone = "+94771234567"
    db = AsyncMock()
    db.execute.side_effect = [_result(user)]
    with pytest.raises(ValidationError):
        await AuthService.update_profile(
            SimpleNamespace(user_id=user.user_id),
            UpdateProfileRequest(alternate_phone="0771234567"),
            db,
        )


@pytest.mark.asyncio
async def test_the_second_number_can_be_added_and_removed():
    user = _account(nic="199512345678")
    user.phone = "+94771234567"
    db = AsyncMock()
    db.execute.side_effect = [_result(user)]
    await AuthService.update_profile(
        SimpleNamespace(user_id=user.user_id),
        UpdateProfileRequest(alternate_phone="0719876543"),
        db,
    )
    assert user.alternate_phone == "+94719876543"

    db.execute.side_effect = [_result(user)]
    await AuthService.update_profile(
        SimpleNamespace(user_id=user.user_id),
        UpdateProfileRequest(alternate_phone=""),
        db,
    )
    assert user.alternate_phone is None


def test_an_order_needs_two_different_reachable_numbers():
    order = CreateOrderRequest(
        delivery_address_id=str(uuid4()),
        contact_phone="077 123 4567",
        alternate_phone="0719876543",
    )
    assert order.contact_phone == "+94771234567"
    assert order.alternate_phone == "+94719876543"

    with pytest.raises(ValueError):
        CreateOrderRequest(
            delivery_address_id=str(uuid4()),
            contact_phone="0771234567",
            alternate_phone="+94771234567",
        )
    with pytest.raises(ValueError):
        CreateOrderRequest(delivery_address_id=str(uuid4()))
