"""
Tests for PricingService's courier-quoted vs. flat-fee-fallback delivery fee
paths (docs/SRIBEESEXPRESS_ECOMMERCE_API.md: an unserviceable area is "a
normal answer, not an error", and we do not block a sale on a courier
outage). No live courier is required for any of these -- that is the point.
"""
from decimal import Decimal

import pytest

from app.services.courier_service import CourierError, CourierUnavailableError
from app.services.pricing_service import PricingService

_ITEMS = [{"product_id": "p1", "price": 100, "quantity": 2}]

# Whatever POST /ecommerce/locations/resolve cached on the address.
_CITY_ID = 42


@pytest.mark.asyncio
async def test_empty_cart_has_zero_delivery_fee():
    breakdown = await PricingService.quote([], delivery_fee=Decimal("350"))
    assert breakdown.delivery_fee == Decimal("0.00")
    assert breakdown.courier_quote_id is None


@pytest.mark.asyncio
async def test_no_city_id_falls_back_to_flat_fee():
    """No delivery address chosen yet (e.g. first quote-preview call)."""
    breakdown = await PricingService.quote(_ITEMS, delivery_fee=Decimal("350"), tax_rate=Decimal("0"))
    assert breakdown.delivery_fee == Decimal("350.00")
    assert breakdown.courier_quote_id is None
    assert breakdown.total == Decimal("550.00")


@pytest.mark.asyncio
async def test_unresolved_address_falls_back_to_flat_fee():
    """
    An address that exists but never resolved to a city_id -- SribeesExpress
    has not configured that post office yet (contract section 5a's
    `not_yet_configured`). The customer still gets a price.
    """
    breakdown = await PricingService.quote(
        _ITEMS,
        delivery_fee=Decimal("350"),
        tax_rate=Decimal("0"),
        city_id=None,
        payment_method="cod",
    )
    assert breakdown.delivery_fee == Decimal("350.00")
    assert breakdown.courier_quote_id is None


@pytest.mark.asyncio
async def test_unconfigured_courier_base_url_falls_back_to_flat_fee(monkeypatch):
    """
    The exact production scenario at first deploy: an address IS resolved
    (city_id present) but COURIER_BASE_URL is still empty because no
    SribeesExpress keys have been issued yet. Checkout must not break.
    """
    from app.config.settings import settings

    monkeypatch.setattr(settings, "courier_base_url", "", raising=False)
    breakdown = await PricingService.quote(
        _ITEMS,
        delivery_fee=Decimal("350"),
        tax_rate=Decimal("0"),
        city_id=_CITY_ID,
        payment_method="cod",
    )
    assert breakdown.delivery_fee == Decimal("350.00")
    assert breakdown.courier_quote_id is None


@pytest.mark.asyncio
async def test_courier_unavailable_error_falls_back_to_flat_fee(monkeypatch):
    async def _raise(*args, **kwargs):
        raise CourierUnavailableError("simulated outage")

    from app.services import courier_service

    monkeypatch.setattr(courier_service.CourierService, "get_rate", _raise)

    breakdown = await PricingService.quote(
        _ITEMS,
        delivery_fee=Decimal("350"),
        tax_rate=Decimal("0"),
        city_id=_CITY_ID,
        payment_method="cod",
    )
    assert breakdown.delivery_fee == Decimal("350.00")
    assert breakdown.courier_quote_id is None


@pytest.mark.asyncio
async def test_structured_courier_error_falls_back_to_flat_fee(monkeypatch):
    """
    A 4xx with a stable error.code (e.g. a revoked key, a bad city_id) is a
    real answer rather than an outage -- but at quote time it must still
    degrade to the flat fee rather than 500 the checkout page.
    """
    async def _raise(*args, **kwargs):
        raise CourierError("unauthorized", "Invalid X-Api-Key", 401)

    from app.services import courier_service

    monkeypatch.setattr(courier_service.CourierService, "get_rate", _raise)

    breakdown = await PricingService.quote(
        _ITEMS,
        delivery_fee=Decimal("350"),
        tax_rate=Decimal("0"),
        city_id=_CITY_ID,
        payment_method="cod",
    )
    assert breakdown.delivery_fee == Decimal("350.00")
    assert breakdown.courier_quote_id is None


@pytest.mark.asyncio
async def test_unserviceable_city_falls_back_to_flat_fee(monkeypatch):
    """`serviceable: false` is a 200, not an error -- and still needs a fee."""
    from app.services import courier_service
    from app.services.courier_service import CourierQuote

    async def _fake_get_rate(*args, **kwargs):
        return CourierQuote(
            quote_id=None,
            amount_lkr=Decimal("0"),
            serviceable=False,
            reason="No delivery to this city.",
        )

    monkeypatch.setattr(courier_service.CourierService, "get_rate", _fake_get_rate)

    breakdown = await PricingService.quote(
        _ITEMS,
        delivery_fee=Decimal("350"),
        tax_rate=Decimal("0"),
        city_id=_CITY_ID,
        payment_method="cod",
    )
    assert breakdown.delivery_fee == Decimal("350.00")
    assert breakdown.courier_quote_id is None


@pytest.mark.asyncio
async def test_courier_quote_used_when_available(monkeypatch):
    from app.services import courier_service
    from app.services.courier_service import CourierQuote

    async def _fake_get_rate(*args, **kwargs):
        return CourierQuote(quote_id="qt_test123", amount_lkr=Decimal("480.00"), serviceable=True)

    monkeypatch.setattr(courier_service.CourierService, "get_rate", _fake_get_rate)

    breakdown = await PricingService.quote(
        _ITEMS,
        delivery_fee=Decimal("350"),
        tax_rate=Decimal("0"),
        city_id=_CITY_ID,
        payment_method="cod",
    )
    assert breakdown.delivery_fee == Decimal("480.00")
    assert breakdown.courier_quote_id == "qt_test123"
    assert breakdown.total == Decimal("680.00")


@pytest.mark.asyncio
async def test_city_id_and_cod_amount_are_sent_to_the_courier(monkeypatch):
    """
    The quote is keyed on city_id alone -- no origin/destination objects
    survive from the old contract -- and COD carries the pre-delivery payable
    as cod_amount even though it is inert for pricing today.
    """
    from app.services import courier_service
    from app.services.courier_service import CourierQuote

    captured = {}

    async def _fake_get_rate(**kwargs):
        captured.update(kwargs)
        return CourierQuote(quote_id="qt_x", amount_lkr=Decimal("400.00"), serviceable=True)

    monkeypatch.setattr(courier_service.CourierService, "get_rate", _fake_get_rate)

    await PricingService.quote(
        _ITEMS,
        delivery_fee=Decimal("350"),
        tax_rate=Decimal("0"),
        city_id=_CITY_ID,
        payment_method="cod",
    )

    assert captured["city_id"] == _CITY_ID
    assert captured["payment_method"] == "cod"
    # 2 x 100, no discount, no tax.
    assert captured["cod_amount"] == Decimal("200")


@pytest.mark.asyncio
async def test_prepaid_sends_zero_cod_amount(monkeypatch):
    from app.services import courier_service
    from app.services.courier_service import CourierQuote

    captured = {}

    async def _fake_get_rate(**kwargs):
        captured.update(kwargs)
        return CourierQuote(quote_id="qt_x", amount_lkr=Decimal("400.00"), serviceable=True)

    monkeypatch.setattr(courier_service.CourierService, "get_rate", _fake_get_rate)

    await PricingService.quote(
        _ITEMS,
        delivery_fee=Decimal("350"),
        tax_rate=Decimal("0"),
        city_id=_CITY_ID,
        payment_method="prepaid",
    )

    assert captured["cod_amount"] == Decimal("0")
