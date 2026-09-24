"""
A "Specific Customers" coupon is for the customers on its list, and nobody else.

The admin form offered the whitelist long before anything stored or checked it,
so a coupon made for five customers worked for everyone with the code. These
use the real Coupon model, not a stand-in: the rule lives on the mapped
columns and the loaded `whitelist` rows, and that mapping is what broke.
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

import pytest

from app.models.coupon import Coupon, CouponWhitelistEntry
from app.services.coupon_service import CouponService, CouponValidationError

NOW = datetime(2026, 9, 25, 12, 0, tzinfo=timezone.utc)


def _coupon(target="specific", entries=()):
    c = Coupon(
        code="FIVEONLY",
        discount_type="fixed",
        discount_value=Decimal("100"),
        min_order_value=Decimal("0"),
        is_active=True,
        used_count=0,
        first_order_only=False,
        valid_from=NOW - timedelta(days=1),
        valid_until=NOW + timedelta(days=1),
        target_customer_type=target,
    )
    c.whitelist = [CouponWhitelistEntry(phone=p, user_id=u) for p, u in entries]
    return c


def _check(coupon, user_id, phone):
    CouponService.validate(
        coupon, Decimal("1000"), NOW, user_id=user_id, user_phone=phone
    )


def test_a_listed_number_may_use_it():
    _check(_coupon(entries=[("+94771234567", None)]), uuid4(), "+94771234567")


def test_the_number_matches_however_the_account_stored_it():
    """Older accounts hold the number as typed. Both sides are compared in
    +94 form, or a listed customer is refused over formatting."""
    _check(_coupon(entries=[("+94771234567", None)]), uuid4(), "0771234567")


def test_the_listed_account_still_matches_after_changing_number():
    me = uuid4()
    _check(_coupon(entries=[("+94771234567", me)]), me, "+94719999999")


def test_anyone_else_is_refused():
    with pytest.raises(CouponValidationError, match="selected customers"):
        _check(_coupon(entries=[("+94771234567", None)]), uuid4(), "+94710000000")


def test_an_empty_list_admits_nobody():
    """A list that failed to save must never read as "everyone"."""
    with pytest.raises(CouponValidationError):
        _check(_coupon(entries=[]), uuid4(), "+94771234567")


def test_no_customer_means_no_coupon():
    with pytest.raises(CouponValidationError):
        _check(_coupon(entries=[("+94771234567", None)]), None, None)


def test_an_all_customers_coupon_is_untouched():
    _check(_coupon(target="all"), uuid4(), None)


def test_a_new_coupon_defaults_to_everyone_in_the_database():
    col = Coupon.__table__.c.target_customer_type
    assert not col.nullable
    assert col.server_default.arg == "all"
