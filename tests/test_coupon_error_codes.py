"""
Every coupon refusal names its rule, and keeps the English it always had.

The code (and its figures) is what lets the refusal be said in the
customer's language; the English message must not change, because app
builds already in the Play Store show it verbatim.
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.models.coupon import Coupon
from app.services.coupon_service import CouponService, CouponValidationError

NOW = datetime(2026, 9, 26, 12, 0, tzinfo=timezone.utc)


def _coupon(**over) -> Coupon:
    c = Coupon(
        code="RICE10",
        discount_type="percentage",
        discount_value=Decimal("10"),
        min_order_value=Decimal("0"),
        is_active=True,
        used_count=0,
        first_order_only=False,
        valid_from=NOW - timedelta(days=1),
        valid_until=NOW + timedelta(days=1),
        target_customer_type="all",
    )
    c.whitelist = []
    for key, value in over.items():
        setattr(c, key, value)
    return c


BRANCH, OTHER = uuid4(), uuid4()

CASES = [
    ({"budget_cap": Decimal("100"), "budget_spent": Decimal("100")}, {}, "budget_ended",
     "This promotion has ended."),
    ({"is_active": False}, {}, "inactive", "This coupon is no longer active."),
    ({"branch_id": OTHER}, {"branch_id": BRANCH}, "wrong_branch",
     "This coupon is not valid for your delivery branch."),
    ({"valid_from": NOW + timedelta(days=2)}, {}, "not_started", "This coupon is not valid yet."),
    ({"valid_until": NOW - timedelta(hours=1)}, {}, "expired", "This coupon has expired."),
    ({"target_user_id": uuid4()}, {"user_id": uuid4()}, "not_yours",
     "This coupon was issued to a specific customer."),
    ({"usage_limit": 5, "used_count": 5}, {}, "usage_limit", "This coupon has reached its usage limit."),
    ({"per_user_limit": 1}, {"user_redemptions": 1}, "per_user_limit",
     "You have already used this coupon the maximum number of times."),
    ({"first_order_only": True}, {"user_has_completed_order": True}, "first_order",
     "This coupon is only valid on a customer's first order."),
    ({"target_customer_type": "specific"}, {"user_id": uuid4(), "user_phone": "+94771234567"},
     "not_whitelisted", "This coupon is only for selected customers."),
    ({"min_order_value": Decimal("1500")}, {}, "min_order",
     "This coupon requires a minimum order of Rs 1500.00."),
]


@pytest.mark.parametrize("coupon_over, call, code, english", CASES, ids=[c[2] for c in CASES])
def test_each_refusal_names_its_rule(coupon_over, call, code, english):
    with pytest.raises(CouponValidationError) as err:
        CouponService.validate(_coupon(**coupon_over), Decimal("1000"), NOW, **call)
    assert err.value.code == code
    assert str(err.value) == english, "the English the app shows must not change"


def test_the_figures_travel_with_the_code():
    with pytest.raises(CouponValidationError) as err:
        CouponService.validate(_coupon(min_order_value=Decimal("1500")), Decimal("1000"), NOW)
    assert err.value.params == {"min_order": Decimal("1500"), "short_by": Decimal("500")}

    with pytest.raises(CouponValidationError) as err:
        CouponService.validate(_coupon(per_user_limit=2), Decimal("1000"), NOW, user_redemptions=2)
    assert err.value.params == {"limit": 2}

    starts = NOW + timedelta(days=3)
    with pytest.raises(CouponValidationError) as err:
        CouponService.validate(_coupon(valid_from=starts), Decimal("1000"), NOW)
    assert err.value.params == {"starts_at": starts}


def test_a_members_only_refusal_carries_the_level():
    tier = SimpleNamespace(name="Gold", min_points=500)
    coupon = _coupon(min_tier_id=uuid4())
    coupon.min_tier = tier
    with pytest.raises(CouponValidationError) as err:
        CouponService.validate(coupon, Decimal("1000"), NOW, user_points=100)
    assert err.value.code == "tier"


def test_an_old_style_refusal_still_works():
    """Callers that raise with only a message get the generic code."""
    err = CouponValidationError("Something else")
    assert (str(err), err.code, err.params) == ("Something else", "invalid", {})
    assert isinstance(err, ValueError), "handlers map it to a 400 through ValueError"
