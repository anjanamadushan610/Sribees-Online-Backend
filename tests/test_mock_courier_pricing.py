"""
Tests for the mock courier's deterministic pricing formula and the
serviceable/error scenarios documented in mock_courier/README.md.
"""
from decimal import Decimal

import pytest

from mock_courier.rates import BASE, COD_FEE_RATE, PER_KG_AFTER_FIRST, quote_amount


def test_first_kilogram_is_free():
    assert quote_amount(Decimal("1"), Decimal("0"), "prepaid") == BASE


def test_under_one_kilogram_still_charges_base_only():
    assert quote_amount(Decimal("0.4"), Decimal("0"), "prepaid") == BASE


def test_weight_surcharge_applies_above_one_kg():
    amount = quote_amount(Decimal("3"), Decimal("0"), "prepaid")
    assert amount == BASE + Decimal("2") * PER_KG_AFTER_FIRST


def test_cod_fee_is_one_percent_of_order_value():
    amount = quote_amount(Decimal("0.5"), Decimal("1000"), "cod")
    assert amount == BASE + Decimal("1000") * COD_FEE_RATE


def test_prepaid_orders_never_get_a_cod_fee():
    amount = quote_amount(Decimal("0.5"), Decimal("1000"), "prepaid")
    assert amount == BASE


@pytest.mark.asyncio
async def test_rates_endpoint_forces_weight_exceeds_maximum():
    from mock_courier.rates import get_rate, RateRequest

    response = await get_rate(
        RateRequest(
            origin={"name": "x"},
            destination={"post_office": "Colombo"},
            parcel={"total_weight_kg": 999, "item_count": 1},
            order_value_lkr=100,
            payment_method="cod",
        )
    )
    assert response.status_code == 422
    assert response.body is not None


@pytest.mark.asyncio
async def test_rates_endpoint_forces_unserviceable():
    from mock_courier.rates import get_rate, RateRequest

    result = await get_rate(
        RateRequest(
            origin={"name": "x"},
            destination={"post_office": "UNSERVICEABLE_TEST"},
            parcel={"total_weight_kg": 1, "item_count": 1},
            order_value_lkr=100,
            payment_method="cod",
        )
    )
    assert result == {"serviceable": False, "reason": "No delivery to this post office."}
