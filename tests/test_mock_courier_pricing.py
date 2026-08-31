"""
Tests for the mock courier's deterministic pricing formula, its location
resolution, and the forceable scenarios documented in mock_courier/README.md.
"""
from decimal import Decimal

import pytest

from mock_courier.rates import AFTER_KG_RATE, FIRST_KG, quote_amount
from mock_courier.state import STORE


def test_first_kilogram_is_the_base_rate():
    assert quote_amount(Decimal("1")) == FIRST_KG


def test_under_one_kilogram_still_charges_the_base_rate():
    assert quote_amount(Decimal("0.4")) == FIRST_KG


def test_weight_surcharge_applies_above_one_kg():
    assert quote_amount(Decimal("3")) == FIRST_KG + Decimal("2") * AFTER_KG_RATE


def test_pricing_is_weight_only():
    """
    The real contract prices on weight alone -- cod_amount and payment_method
    are inert (section 6). A mock that priced COD differently would hide a
    production bug behind a green test.
    """
    assert quote_amount(Decimal("2.5")) == FIRST_KG + Decimal("1.5") * AFTER_KG_RATE


@pytest.mark.asyncio
async def test_rates_rejects_out_of_range_weight():
    from mock_courier.rates import RateRequest, get_rate

    response = await get_rate(RateRequest(city_id=42, weight_kg=0))
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_sandbox_weight_trigger_forces_unserviceable():
    """The contract's own documented sandbox trigger (section 11)."""
    from mock_courier.rates import RateRequest, get_rate

    result = await get_rate(RateRequest(city_id=42, weight_kg=900))
    assert result["serviceable"] is False
    assert "sandbox" in result["reason"]


@pytest.mark.asyncio
async def test_unknown_city_is_unserviceable_not_an_error():
    from mock_courier.rates import RateRequest, get_rate

    result = await get_rate(RateRequest(city_id=9999, weight_kg=1))
    assert result["serviceable"] is False
    assert result["quote_id"] is None


@pytest.mark.asyncio
async def test_serviceable_quote_returns_an_expiry_and_breakdown():
    from mock_courier.rates import RateRequest, get_rate

    result = await get_rate(RateRequest(city_id=42, weight_kg=2.5))
    assert result["serviceable"] is True
    assert result["quote_id"].startswith("qt_")
    assert result["amount_lkr"] == float(FIRST_KG + Decimal("1.5") * AFTER_KG_RATE)
    assert result["expires_at"]
    assert STORE.quotes[result["quote_id"]]["used"] is False


# ---------------------------------------------------------------------------
# Section 5a -- POST /locations/resolve
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_returns_a_city_id_for_a_known_post_office():
    from mock_courier.rates import ResolveRequest, resolve_location

    result = await resolve_location(ResolveRequest(post_office="Welipenna", district="Kalutara"))
    assert result["resolved"] is True
    assert result["city_id"] == 43


@pytest.mark.asyncio
async def test_resolve_is_never_a_404():
    """An unresolvable location is a normal 200 answer, not an error."""
    from mock_courier.rates import ResolveRequest, resolve_location

    result = await resolve_location(ResolveRequest(post_office="Nowhere At All"))
    assert result["resolved"] is False
    assert result["reason"] == "post_office_not_found"


@pytest.mark.asyncio
async def test_duplicate_post_office_is_ambiguous_without_a_district():
    from mock_courier.rates import ResolveRequest, resolve_location

    result = await resolve_location(ResolveRequest(post_office="Nugegoda"))
    assert result["resolved"] is False
    assert result["reason"] == "ambiguous_post_office"
    assert len(result["candidates"]) == 2


@pytest.mark.asyncio
async def test_district_disambiguates_a_duplicate_post_office():
    from mock_courier.rates import ResolveRequest, resolve_location

    result = await resolve_location(ResolveRequest(post_office="Nugegoda", district="Gampaha"))
    assert result["resolved"] is True
    assert result["city_id"] == 47


@pytest.mark.asyncio
async def test_known_but_unconfigured_post_office_reports_its_own_reason():
    """
    `not_yet_configured` is a SribeesExpress ops ticket; `post_office_not_found`
    is the customer's typo. The two must not collapse into one answer.
    """
    from mock_courier.rates import ResolveRequest, resolve_location

    result = await resolve_location(ResolveRequest(post_office="Medawachchiya"))
    assert result["resolved"] is False
    assert result["reason"] == "not_yet_configured"
