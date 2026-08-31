"""
Tests for UserCouponService's pure state-derivation logic (``_format``).

collect()/assert_can_redeem()/record_redemption() need a real DB session and
are integration-tested against a database (this repo has no test-DB infra yet —
see tests/README.md).
"""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.services.user_coupon_service import UserCouponService

_NOW = datetime(2026, 6, 15, tzinfo=timezone.utc)


def _coupon(**over):
    base = dict(
        coupon_id="11111111-1111-1111-1111-111111111111",
        code="SAVE10",
        description="Ten off",
        discount_type="percentage",
        discount_value=10,
        min_order_value=0,
        max_discount_amount=None,
        per_user_limit=1,
        is_active=True,
        valid_from=_NOW - timedelta(days=1),
        valid_until=_NOW + timedelta(days=10),
    )
    base.update(over)
    return SimpleNamespace(**base)


def _row(times_redeemed=0):
    return SimpleNamespace(
        times_redeemed=times_redeemed,
        collected_at=_NOW - timedelta(hours=1),
    )


def fmt(coupon, row):
    return UserCouponService._format(coupon, row, _NOW)


def test_uncollected_coupon_is_available_but_not_usable():
    out = fmt(_coupon(), None)
    assert out["state"] == "available"
    assert out["collected"] is False
    assert out["usable"] is False


def test_collected_in_window_is_usable():
    out = fmt(_coupon(), _row(times_redeemed=0))
    assert out["state"] == "collected"
    assert out["usable"] is True


def test_per_user_limit_reached_is_used_and_not_usable():
    out = fmt(_coupon(per_user_limit=1), _row(times_redeemed=1))
    assert out["state"] == "used"
    assert out["usable"] is False


def test_unlimited_per_user_stays_usable_after_redemptions():
    out = fmt(_coupon(per_user_limit=None), _row(times_redeemed=5))
    assert out["state"] == "collected"
    assert out["usable"] is True


def test_expired_coupon_is_never_usable_even_if_collected():
    out = fmt(_coupon(valid_until=_NOW - timedelta(days=1)), _row())
    assert out["state"] == "expired"
    assert out["usable"] is False


def test_inactive_coupon_is_expired_state():
    out = fmt(_coupon(is_active=False), _row())
    assert out["state"] == "expired"
    assert out["usable"] is False


def test_not_started_coupon_is_not_usable():
    out = fmt(_coupon(valid_from=_NOW + timedelta(days=2)), _row())
    assert out["usable"] is False


def test_customer_payload_hides_internal_fields():
    out = fmt(_coupon(), _row())
    for leaked in ("used_count", "usage_limit", "is_public", "created_at"):
        assert leaked not in out
