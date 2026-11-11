"""The offer loss cap's pure rules (app/services/offer_cap_service.py)."""
from decimal import Decimal
from uuid import uuid4

import pytest

from app.services import offer_cap_service as s

OFFER = uuid4()
CAP = {OFFER: Decimal("1000.00")}
SUGAR = (OFFER, Decimal("61.00"))
SALMON = (OFFER, Decimal("160.00"))


def test_sixteen_kilos_fit_and_say_reached():
    v = s.judge(CAP, {}, [(SUGAR, 16)])
    assert not v.over and v.reached and v.takes[OFFER] == Decimal("976.00")


def test_the_seventeenth_crosses_the_cap():
    assert s.judge(CAP, {}, [(SUGAR, 17)]).over


def test_the_room_is_shared_and_earlier_orders_count():
    assert not s.judge(CAP, {}, [(SALMON, 1), (SUGAR, 13)]).over
    assert s.judge(CAP, {}, [(SALMON, 1), (SUGAR, 14)]).over
    assert s.judge(CAP, {OFFER: Decimal("953.00")}, [(SUGAR, 1)]).over


def test_lines_outside_offers_are_ignored():
    v = s.judge(CAP, {}, [(None, 500)])
    assert not v.over and not v.reached and v.takes == {}


def test_discount_lands_exactly_on_the_offer_price():
    for regular, offer in [("560", "400"), ("260", "199"), ("480", "390"), ("290", "200"), ("270", "198")]:
        pct = s.discount_for(Decimal(regular), Decimal(offer))
        assert round(float(regular) * (1 - pct / 100), 2) == float(offer)


def test_with_quantity_adds_or_sets():
    items = [{"product_id": "a", "variant_id": None, "quantity": 2}]
    assert s.with_quantity(items, "a", None, 3, add=True)[0]["quantity"] == 5
    assert s.with_quantity(items, "a", None, 3, add=False)[0]["quantity"] == 3
    assert s.with_quantity(items, "b", None, 1, add=True)[-1]["product_id"] == "b"


@pytest.mark.parametrize("lang,needle", [("si", "සීමාව"), ("en", "offer limit"), ("ta", "வரம்பு")])
def test_message_language_and_no_amount(lang, needle):
    text = s.limit_message(lang)
    assert needle in text and not any(ch.isdigit() for ch in text)
