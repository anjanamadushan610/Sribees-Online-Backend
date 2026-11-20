"""
The per-product commission arithmetic (app/services/referral_commission).

Pure functions, so these pin the money rules down exactly: every number here
is worked out by hand in the comment beside it.
"""
import random
import uuid
from decimal import Decimal

import pytest

from app.services.referral_commission import (
    LevelPolicy,
    SaleLine,
    allocate_discount,
    build_lines,
    kept_amount,
    money,
)
from app.services.referral_commission.config import _parse
from app.services.referral_commission.presenter import offer_text
from app.services.referral_commission.rate_admin import RateChangeError, parse_rate
from app.services.referral_commission.rates import effective_rate

D = Decimal


def _line(gross, rate, *, qty=1, coupon="0", name="Rice"):
    return SaleLine(
        order_item_id=uuid.uuid4(),
        product_id=uuid.uuid4(),
        product_name=name,
        quantity=qty,
        gross=D(gross),
        coupon_discount=D(coupon),
        rate=D(rate),
    )


# --------------------------------------------------------------- allocation

def test_discount_is_shared_in_proportion_to_value():
    # Rs 100 off 300 + 100: three quarters to the first line.
    assert allocate_discount([D("300"), D("100")], [True, True], D("100")) == [D("75.00"), D("25.00")]


def test_only_eligible_lines_take_a_share():
    # A coupon for rice only: the oil line keeps its full price.
    assert allocate_discount([D("500"), D("200")], [True, False], D("50")) == [D("50.00"), D("0.00")]


def test_leftover_cents_go_to_the_largest_remainders_and_the_sum_is_exact():
    # Rs 10 over three equal lines: 3.33 each leaves 1 cent, to the first.
    shares = allocate_discount([D("100")] * 3, [True] * 3, D("10"))
    assert shares == [D("3.34"), D("3.33"), D("3.33")]
    assert sum(shares) == D("10.00")


def test_a_discount_never_exceeds_the_eligible_goods():
    assert allocate_discount([D("40"), D("500")], [True, False], D("100")) == [D("40.00"), D("0.00")]


@pytest.mark.parametrize("discount", ["0", "-5"])
def test_no_discount_means_no_shares(discount):
    assert allocate_discount([D("100"), D("50")], [True, True], D(discount)) == [D("0.00"), D("0.00")]


def test_lengths_must_match():
    with pytest.raises(ValueError):
        allocate_discount([D("1")], [True, False], D("1"))


def test_allocation_is_exact_over_many_random_baskets():
    rng = random.Random(81)
    for _ in range(2000):
        n = rng.randint(1, 8)
        gross = [money(D(rng.randint(1, 500_000)) / 100) for _ in range(n)]
        eligible = [rng.random() < 0.7 for _ in range(n)]
        pool = sum((g for g, ok in zip(gross, eligible, strict=True) if ok), D("0"))
        discount = money(D(rng.randint(0, 600_000)) / 100)
        shares = allocate_discount(gross, eligible, discount)
        assert sum(shares) == min(discount, pool)
        for share, g, ok in zip(shares, gross, eligible, strict=True):
            assert D("0") <= share <= (g if ok else D("0"))


# --------------------------------------------------------------- lines

def test_each_line_earns_its_own_rate_on_what_was_paid():
    lines = [
        _line("1000", "5", qty=2, coupon="100"),  # 900 x 5% = 45.00
        _line("300", "0"),                         # 0% earns nothing
        _line("250", "2.5"),                       # 250 x 2.5% = 6.25
    ]
    built = build_lines(lines, LevelPolicy(D("0.5")).for_level(1))
    assert [(b.basis, b.amount) for b in built] == [(D("900.00"), D("45.00")), (D("250.00"), D("6.25"))]


def test_level_two_earns_half_of_partner_share():
    lines = [_line("1000", "5"), _line("333", "3")]
    l2 = build_lines(lines, LevelPolicy(D("0.5")).for_level(2))
    # A 50% Master-to-Partner ratio makes the Master one third of the pool.
    assert [b.amount for b in l2] == [D("16.67"), D("3.33")]
    assert [b.rate for b in l2] == [D("1.667"), D("1.000")]


def test_level_two_rate_keeps_three_places():
    # Half of the Partner's one-third pool share is 0.75% of the product.
    assert LevelPolicy(D("0.5")).rate(2, D("2.25")) == D("0.750")


def test_rounding_is_half_up_per_line():
    # 0.10 x 5% = 0.005 -> 0.01
    assert build_lines([_line("0.10", "5")], lambda r: r)[0].amount == D("0.01")


def test_a_fully_couponed_line_earns_nothing():
    assert build_lines([_line("100", "10", coupon="100")], lambda r: r) == []


def test_there_is_no_third_level():
    with pytest.raises(ValueError):
        LevelPolicy(D("0.5")).rate(3, D("5"))


@pytest.mark.parametrize("share", ["-0.1", "1.5"])
def test_level2_share_must_be_a_fraction(share):
    with pytest.raises(ValueError):
        LevelPolicy(D(share))


# --------------------------------------------------------------- returns

def test_returning_some_units_keeps_the_rest_of_the_commission():
    # Basis 900 over 3 units at 5%: keep 2 units -> 600 x 5% = 30.00
    assert kept_amount(D("900"), D("5"), 3, 1) == D("30.00")


def test_returning_everything_keeps_nothing():
    assert kept_amount(D("900"), D("5"), 3, 3) == D("0.00")
    assert kept_amount(D("900"), D("5"), 3, 7) == D("0.00")


def test_kept_amount_is_stable_when_recomputed():
    once = kept_amount(D("99.99"), D("3.333"), 7, 2)
    assert kept_amount(D("99.99"), D("3.333"), 7, 2) == once


# --------------------------------------------------------------- rates & settings

@pytest.mark.parametrize(
    "branch,product,expected",
    [(D("4"), D("2"), D("2")), (None, D("2"), D("2")), (None, None, D("0")), (D("4"), None, D("0")), (D("0"), D("5"), D("0"))],
)
def test_branch_then_product_then_zero(branch, product, expected):
    assert effective_rate(branch, product) == expected


@pytest.mark.parametrize("raw", ["-1", "100.01", "abc", "1.234", "NaN", "Infinity"])
def test_bad_rates_are_refused(raw):
    with pytest.raises(RateChangeError):
        parse_rate(raw)


def test_a_rate_may_be_cleared_or_set_to_two_places():
    assert parse_rate(None) is None
    assert parse_rate("7.5") == D("7.50")
    assert parse_rate(0) == D("0.00")


@pytest.mark.parametrize("raw", [None, "", "x", "150"])
def test_a_bad_stored_setting_falls_back(raw):
    assert _parse(raw, D("0"), D("100"), D("10"), "k") == D("10")


# --------------------------------------------------------------- wording

def test_offer_text_is_written_by_the_server_in_the_callers_language():
    assert offer_text(D("5.00"), audience="partner", accept_language=None) == (
        "Earn commission on selected products your customers buy."
    )
    si = offer_text(D("2.50"), audience="friend", accept_language="si")
    assert "කොමිස්" in si and "%" not in si, "no rate is shown: it differs per product"
    assert "None are on offer" in offer_text(D("0"), audience="friend", accept_language="en")


@pytest.mark.parametrize("share", ["0", "0.25", "0.3", "0.5", "1"])
def test_product_commission_is_one_shared_pool(share):
    from app.services.referral_commission import build_shared_lines
    direct, master = build_shared_lines([_line("1000", "1")], D(share))
    assert sum((line.amount for line in direct + master), D("0")) == D("10.00")
    if share == "0.25":
        assert direct[0].amount == D("8.00") and master[0].amount == D("2.00")


def test_small_pools_and_returns_conserve_every_cent():
    from app.services.referral_commission import build_shared_lines, kept_shared_amount
    rng = random.Random(120)
    for _ in range(1000):
        qty = rng.randint(1, 20)
        line = _line(str(D(rng.randint(1, 200000)) / 100), str(D(rng.randint(1, 10000)) / 100), qty=qty)
        share = D(rng.randint(0, 1000)) / 1000
        direct, master = build_shared_lines([line], share)
        pool_share = share / (D("1") + share)
        pool = money(line.basis * line.rate / 100)
        assert sum((entry.amount for entry in direct + master), D("0")) == pool
        previous = [sum((entry.amount for entry in direct), D("0")), sum((entry.amount for entry in master), D("0"))]
        for returned in range(qty + 1):
            kept = [kept_shared_amount(line.basis, line.rate, pool_share, level, qty, returned) for level in (1, 2)]
            assert sum(kept) == kept_amount(line.basis, line.rate, qty, returned)
            assert all(D("0") <= current <= old for current, old in zip(kept, previous, strict=False))
            previous = kept
    direct, master = build_shared_lines([_line("0.10", "5")], D("0.5"))
    assert direct[0].amount == D("0.01") and master == []
