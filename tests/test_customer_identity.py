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
from app.utils.identity import missing_profile_fields, normalize_mobile, normalize_nic


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
        ("952345678v", "952345678V"),
        ("952345678X", "952345678X"),
        ("19 9512 345678", "199512345678"),
        ("1995-2345-6789", "199523456789"),
    ],
)
def test_nic_is_stored_in_one_canonical_shape(typed, stored):
    # A UNIQUE column is only unique if "952345678v" and "952345678V" are the
    # same string by the time they reach it.
    assert normalize_nic(typed) == stored


@pytest.mark.parametrize(
    "bad", ["12345678V", "9523456780", "952345678Y", "abcdefghi", "", "9523456780123"]
)
def test_nic_rejects_anything_that_is_not_a_nic(bad):
    with pytest.raises(ValueError):
        normalize_nic(bad)


@pytest.mark.parametrize(
    "fake",
    [
        "123456789V",    # born 1912 - older than anyone who could hold one
        "999999999V",    # day 999
        "990009999V",    # day 000
        "199537012345",  # day 370
        "199586712345",  # day 867 (a woman's 367)
        "203012345678",  # born in the future
        "201512345678",  # born 2015 - too young for a NIC
        "199506012345",  # 29 February 1995, which never happened
        "199000001234",  # the synthetic backfill pattern (day 000)
    ],
)
def test_nic_with_an_impossible_birth_date_is_refused(fake):
    with pytest.raises(ValueError):
        normalize_nic(fake)


@pytest.mark.parametrize(
    "real",
    [
        "199606012345",  # 29 February 1996, a leap year
        "200036612345",  # 31 December 2000 (day 366 in a leap year)
        "857501234V",    # a woman born 1985, day 250
        "200586612345",  # a woman, day 366
    ],
)
def test_nic_with_a_possible_birth_date_is_accepted(real):
    assert normalize_nic(real) == real


@pytest.mark.parametrize(
    "typed", ["0771234567", "771234567", "+94771234567", "94 77 123 4567"]
)
def test_every_way_of_typing_a_mobile_lands_on_the_same_number(typed):
    assert normalize_mobile(typed) == "+94771234567"


@pytest.mark.parametrize("bad", ["0112345678", "07712345", "0779523456780", ""])
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
        UpdateProfileRequest(nic="952345678v"),
        db,
    )
    assert user.nic == "952345678V"
    assert response.user.nic == "952345678V"


@pytest.mark.asyncio
async def test_an_account_cannot_rewrite_its_own_nic():
    """Identity a customer can edit is not identity. Support does this."""
    user = _account(nic="199512345678")
    db = AsyncMock()
    db.execute.side_effect = [_result(user)]
    with pytest.raises(ValidationError):
        await AuthService.update_profile(
            SimpleNamespace(user_id=user.user_id),
            UpdateProfileRequest(nic="952345678V"),
            db,
        )
    assert user.nic == "199512345678"


@pytest.mark.asyncio
async def test_two_accounts_cannot_claim_the_same_nic():
    user = _account(nic=None)
    other = _account(nic="952345678V")
    db = AsyncMock()
    db.execute.side_effect = [_result(user), _result(other)]
    with pytest.raises(DuplicateError):
        await AuthService.update_profile(
            SimpleNamespace(user_id=user.user_id),
            UpdateProfileRequest(nic="952345678V"),
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


@pytest.mark.asyncio
async def test_a_half_finished_account_is_asked_only_for_what_it_lacks():
    """
    An account opened on the website and signed into on the app is the same
    row. Each client asks for the fields named here and no others, so what
    the first one saved is never asked for -- or written over -- again.
    """
    user = _account(nic="199512345678")
    db = AsyncMock()
    db.execute.side_effect = [_result(user)]
    response = await AuthService.get_profile(SimpleNamespace(user_id=user.user_id), db)
    assert response.user.missing_profile_fields == ["email"]
    # The clients read it under its camelCase name, like every other field.
    assert response.model_dump(by_alias=True)["user"]["missingProfileFields"] == ["email"]

    user.email = "customer@example.com"
    db.execute.side_effect = [_result(user)]
    response = await AuthService.get_profile(SimpleNamespace(user_id=user.user_id), db)
    assert response.user.missing_profile_fields == []


def test_a_new_account_owes_its_name_its_nic_and_its_email():
    assert missing_profile_fields(full_name="", email=None, nic=None) == [
        "full_name",
        "nic",
        "email",
    ]
    assert missing_profile_fields(full_name="  ", email="a@b.lk", nic="952345678V") == [
        "full_name"
    ]


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
