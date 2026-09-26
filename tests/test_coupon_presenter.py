"""
The coupon cards the app draws, decided on the server.

The app renders these fields and decides nothing, so every rule the phone
used to carry is pinned here instead: the words on the card in three
languages, which section it goes in, what its button does, why it is locked,
and a discount figure that is checkout's own.
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

import pytest

from app.models.coupon import Coupon, CouponCategory
from app.models.loyalty import LoyaltyTier
from app.services import coupon_presenter as cp
from app.services.coupon_service import CouponValidationError
from app.services.pricing_service import PricingService

NOW = datetime(2026, 9, 26, 6, 0, tzinfo=timezone.utc)
RICE, SOAP = str(uuid4()), str(uuid4())


def _coupon(**over) -> Coupon:
    c = Coupon(
        coupon_id=uuid4(),
        code="RICE10",
        description="Ten off rice",
        discount_type="percentage",
        discount_value=Decimal("10"),
        min_order_value=Decimal("0"),
        max_discount_amount=None,
        is_active=True,
        used_count=0,
        first_order_only=False,
        per_user_limit=None,
        valid_from=NOW - timedelta(days=1),
        valid_until=datetime(2026, 10, 4, 20, 0, tzinfo=timezone.utc),  # 5 Oct in Colombo
        target_customer_type="all",
    )
    c.whitelist = []
    c.eligible_categories = []
    c.eligible_products = []
    c.min_tier = None
    for key, value in over.items():
        setattr(c, key, value)
    return c


def _line(price, qty=1, category=RICE):
    return {"product_id": str(uuid4()), "category_id": category, "price": price, "quantity": qty}


# --- words on the card ------------------------------------------------------


@pytest.mark.parametrize(
    "lang, pct, fixed",
    [
        ("en", "10% OFF", "Rs. 500 OFF"),
        ("si", "10% වට්ටම්", "රු. 500 වට්ටම්"),
        ("ta", "10% தள்ளுபடி", "ரூ. 500 தள்ளுபடி"),
    ],
)
def test_the_label_is_in_the_customers_language(lang, pct, fixed):
    assert cp.label(_coupon(), lang) == pct
    assert cp.label(_coupon(discount_type="fixed", discount_value=Decimal("500")), lang) == fixed


def test_a_fractional_percentage_keeps_its_decimals():
    assert cp.label(_coupon(discount_value=Decimal("12.50")), "en") == "12.5% OFF"


def test_money_drops_empty_cents_and_groups_thousands():
    assert cp.money(Decimal("2000")) == "2,000"
    assert cp.money(Decimal("1250.5")) == "1,250.50"


def test_terms_name_every_rule_the_customer_must_meet():
    tier = LoyaltyTier(name="Gold", name_si="රන්", name_ta=None, color="#D4AF37", min_points=500)
    coupon = _coupon(
        min_order_value=Decimal("2000"),
        max_discount_amount=Decimal("500"),
        first_order_only=True,
        per_user_limit=1,
        min_tier=tier,
        eligible_categories=[CouponCategory(category_id=RICE)],
    )
    assert cp.terms(coupon, "en", category_names={RICE: "Rice"}) == [
        "Min. order Rs. 2,000",
        "Max discount Rs. 500",
        "Only on Rice",
        "First order only",
        "Once per customer",
        "Gold members and above",
        "Valid till 5 Oct 2026",
    ]
    si = cp.terms(coupon, "si", category_names={RICE: "සහල්"})
    assert "සහල් සඳහා පමණයි" in si
    assert "රන් සහ ඉහළ සාමාජිකයින්ට" in si
    assert "2026-10-05 දක්වා වලංගුයි" in si


def test_a_fixed_coupon_has_no_max_discount_line():
    coupon = _coupon(discount_type="fixed", discount_value=Decimal("300"), max_discount_amount=Decimal("300"))
    assert not any("Max discount" in t for t in cp.terms(coupon, "en"))


def test_every_language_has_every_line():
    for lang in ("si", "ta"):
        missing = set(cp._COPY["en"]) - set(cp._COPY[lang])
        assert not missing, (lang, missing)


# --- the discount is checkout's own ------------------------------------------


def test_the_estimate_is_the_figure_checkout_charges():
    coupon = _coupon(max_discount_amount=Decimal("150"))
    items = [_line(1000, 2)]
    evaluation = cp.evaluate(coupon, items, now=NOW)
    assert evaluation.eligible
    charged = PricingService._discount(
        Decimal("2000"),
        {
            "discount_type": "percentage",
            "discount_value": Decimal("10"),
            "max_discount_amount": Decimal("150"),
            "eligible_subtotal": Decimal("2000"),
        },
    )
    assert evaluation.discount == charged == Decimal("150.00")


def test_a_category_coupon_discounts_only_its_category():
    """The phone ignored this and promised 10% of the whole cart."""
    coupon = _coupon(eligible_categories=[CouponCategory(category_id=RICE)])
    evaluation = cp.evaluate(coupon, [_line(1000), _line(4000, category=SOAP)], now=NOW)
    assert evaluation.discount == Decimal("100.00")


def test_quick_sale_items_count_like_any_other():
    coupon = _coupon(exclude_quick_sale=True)
    item = {**_line(1000), "is_on_sale": True}
    assert cp.evaluate(coupon, [item], now=NOW).discount == Decimal("100.00")


# --- why it is locked --------------------------------------------------------


def test_short_of_the_minimum_says_how_much_more():
    coupon = _coupon(min_order_value=Decimal("3000"))
    evaluation = cp.evaluate(coupon, [_line(1800)], now=NOW)
    assert not evaluation.eligible
    assert cp.reason(evaluation.error, "en") == "Add Rs. 1,200 more to unlock this offer."
    assert cp.reason(evaluation.error, "si") == "මෙම දීමනාව ලබා ගැනීමට තව රු. 1,200ක් එකතු කරන්න."


def test_short_of_a_category_minimum_asks_for_eligible_items():
    coupon = _coupon(
        min_order_value=Decimal("3000"), eligible_categories=[CouponCategory(category_id=RICE)]
    )
    evaluation = cp.evaluate(coupon, [_line(1000), _line(9000, category=SOAP)], now=NOW)
    assert (
        cp.reason(evaluation.error, "en", restricted=True)
        == "Add Rs. 2,000 more of eligible items to unlock this offer."
    )


def test_nothing_eligible_is_not_a_minimum_problem():
    """Adding more soap never unlocks a rice coupon; saying "add Rs X" would."""
    coupon = _coupon(
        min_order_value=Decimal("500"), eligible_categories=[CouponCategory(category_id=RICE)]
    )
    evaluation = cp.evaluate(coupon, [_line(9000, category=SOAP)], now=NOW)
    assert evaluation.error.code == "no_eligible_items"
    assert cp.reason(evaluation.error, "ta") == "இந்தப் பொருட்கள் எதுவும் இந்த சலுகைக்குத் தகுதியானவை அல்ல."


def test_browsing_checks_every_rule_but_the_basket():
    coupon = _coupon(min_order_value=Decimal("5000"))
    assert cp.evaluate(coupon, None, now=NOW).eligible
    used = cp.evaluate(_coupon(per_user_limit=1), None, now=NOW, user_redemptions=1)
    assert used.error.code == "per_user_limit"


def test_a_rule_without_copy_still_says_something():
    assert cp.reason(CouponValidationError("Brand new rule.", "brand_new"), "si") == "Brand new rule."


def test_not_started_names_the_day():
    coupon = _coupon(valid_from=datetime(2026, 10, 1, 3, 0, tzinfo=timezone.utc))
    evaluation = cp.evaluate(coupon, None, now=NOW)
    assert cp.reason(evaluation.error, "en") == "This offer starts on 1 Oct 2026."


# --- where the card goes and what its button does ----------------------------


def _card(coupon, items=None, basket=True, collected=True, applied=False, lang="en", **facts):
    evaluation = cp.evaluate(coupon, items, now=NOW, **facts)
    return cp.present(
        coupon, lang, evaluation=evaluation, collected=collected, applied=applied, basket=basket
    )


def test_an_eligible_coupon_can_be_used_and_says_what_it_saves():
    card = _card(_coupon(), [_line(2000)])
    assert (card["section"], card["action"], card["status"]["key"]) == ("eligible", "use", "ready")
    assert card["estimated_discount"] == 200.0
    assert card["saving"] == "You save Rs. 200"
    assert card["dimmed"] is False


def test_an_uncollected_eligible_offer_goes_straight_to_use():
    card = _card(_coupon(), [_line(2000)], collected=False)
    assert (card["section"], card["action"]) == ("eligible", "use")


def test_a_locked_coupon_explains_itself_and_has_no_use_button():
    card = _card(_coupon(min_order_value=Decimal("3000")), [_line(1000)])
    assert (card["section"], card["action"]) == ("locked", None)
    assert card["reason"] == "Add Rs. 2,000 more to unlock this offer."
    assert card["dimmed"] is True


def test_a_locked_offer_not_yet_saved_can_still_be_collected():
    card = _card(_coupon(min_order_value=Decimal("3000")), [_line(1000)], collected=False)
    assert (card["section"], card["action"]) == ("locked", "collect")


def test_the_applied_coupon_can_be_removed_even_when_it_stopped_applying():
    card = _card(_coupon(min_order_value=Decimal("3000")), [_line(1000)], applied=True)
    assert (card["section"], card["action"], card["status"]["key"]) == ("locked", "remove", "applied")
    assert card["reason"]


def test_browsing_sorts_into_collect_and_wallet():
    assert _card(_coupon(), basket=False, collected=False)["section"] == "collect"
    assert _card(_coupon(), basket=False, collected=False)["action"] == "collect"
    wallet = _card(_coupon(), basket=False, collected=True)
    assert (wallet["section"], wallet["action"], wallet["status"]["text"]) == ("wallet", None, "READY")


def test_a_used_coupon_reads_used_in_the_wallet():
    card = _card(_coupon(per_user_limit=1), basket=False, user_redemptions=1, lang="si")
    assert card["status"] == {"key": "used", "text": "භාවිත කළා"}
    assert card["dimmed"] is True
    assert card["action"] is None


def test_sections_come_in_display_order_with_titles():
    cards = [{"section": "wallet"}, {"section": "eligible"}, {"section": "locked"}]
    assert cp.sections(cards, "ta") == [
        {"key": "eligible", "title": "தகுதியான சலுகைகள்"},
        {"key": "locked", "title": "மற்ற கூப்பன்கள்"},
        {"key": "wallet", "title": "எனது கூப்பன்கள்"},
    ]
