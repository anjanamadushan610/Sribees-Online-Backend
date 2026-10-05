"""
Tests for customer primary phone number change, OTP verification, and rate limiting.
"""
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from app.core.exceptions import DuplicateError, ValidationError
from app.models.user import User, UserPhoneChangeHistory
from app.schemas.auth import UpdateProfileRequest
from app.services.auth_service import AuthService


def _result(value):
    row = MagicMock()
    row.scalar_one_or_none.return_value = value
    row.scalar_one.return_value = value
    row.scalar.return_value = value
    return row


def _scalar_result(val):
    row = MagicMock()
    row.scalar.return_value = val
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


@pytest.mark.asyncio
async def test_patch_profile_blocks_direct_phone_overwrite():
    """Direct phone change in PATCH /auth/me is blocked; OTP flow must be used."""
    user = _account(phone="+94770000001")
    db = AsyncMock()
    db.execute.return_value = _result(user)

    with pytest.raises(ValidationError) as exc:
        await AuthService.update_profile(
            SimpleNamespace(user_id=user.user_id),
            UpdateProfileRequest(phone="+94770000002"),
            db,
        )
    assert "Primary phone number must be updated using the phone verification flow" in exc.value.message


@pytest.mark.asyncio
async def test_phone_change_quota_calculation():
    """Daily limit is 2 and monthly limit is 4."""
    user = _account(phone="+94770000001")
    db = AsyncMock()
    # Daily count: 1, Monthly count: 2
    db.execute.side_effect = [_scalar_result(1), _scalar_result(2)]

    status = await AuthService.get_phone_change_status(user.user_id, db)
    assert status["daily_remaining"] == 1
    assert status["monthly_remaining"] == 2
    assert status["can_change"] is True


@pytest.mark.asyncio
async def test_request_phone_change_rejects_same_number():
    """Reject if the requested number is identical to the current number."""
    user = _account(phone="+94770000001")
    db = AsyncMock()
    redis = AsyncMock()

    with pytest.raises(ValidationError) as exc:
        await AuthService.request_phone_change(
            user,
            "077 000 0001",
            client_ip="127.0.0.1",
            lang="en",
            db=db,
            redis=redis,
        )
    assert "already your current phone number" in exc.value.message


@pytest.mark.asyncio
async def test_request_phone_change_rejects_duplicate_number():
    """Reject if another active user already owns the target number."""
    user = _account(phone="+94770000001")
    other_user = _account(phone="+94779999999")
    db = AsyncMock()
    # Duplicate check finds other_user
    db.execute.return_value = _result(other_user)
    redis = AsyncMock()

    with pytest.raises(DuplicateError) as exc:
        await AuthService.request_phone_change(
            user,
            "077 999 9999",
            client_ip="127.0.0.1",
            lang="en",
            db=db,
            redis=redis,
        )
    assert "already registered to another account" in str(exc.value)


@pytest.mark.asyncio
async def test_verify_phone_change_updates_number_and_logs():
    """Verifying valid OTP updates phone and records change history."""
    user = _account(phone="+94770000001")
    db = AsyncMock()
    # Collision check: None; Re-fetch user: user
    db.execute.side_effect = [_result(None), _result(user)]
    redis = AsyncMock()
    redis.get.return_value = "+94778888888"

    with patch("app.services.otp_service.OTPService.verify_otp", return_value=True):
        res = await AuthService.verify_phone_change(
            user,
            "077 888 8888",
            "123456",
            client_ip="1.2.3.4",
            db=db,
            redis=redis,
        )

    assert res is not None

    assert user.phone == "+94778888888"
    assert user.is_phone_verified is True
    assert db.add.called
    added_obj = db.add.call_args[0][0]
    assert isinstance(added_obj, UserPhoneChangeHistory)
    assert added_obj.old_phone == "+94770000001"
    assert added_obj.new_phone == "+94778888888"
