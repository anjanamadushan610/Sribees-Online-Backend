"""
Tests for PricingService's courier-quoted vs. flat-fee-fallback delivery fee
paths (docs/COURIER_API.md section 8: "we will not block a sale on a courier
outage"). No live courier is required for any of these -- that is the point.
"""
from decimal import Decimal

import pytest

from app.services.courier_service import CourierUnavailableError
from app.services.pricing_service import PricingService

_ITEMS = [{"product_id": "p1", "price": 100, "quantity": 2}]


@pytest.mark.asyncio
async def test_empty_cart_has_zero_delivery_fee():
    breakdown = await PricingService.quote([], delivery_fee=Decimal("350"))
    assert breakdown.delivery_fee == Decimal("0.00")
    assert breakdown.courier_quote_id is None


@pytest.mark.asyncio
async def test_no_origin_or_destination_falls_back_to_flat_fee():
    """No delivery address chosen yet (e.g. first quote-preview call)."""
    breakdown = await PricingService.quote(_ITEMS, delivery_fee=Decimal("350"), tax_rate=Decimal("0"))
    assert breakdown.delivery_fee == Decimal("350.00")
    assert breakdown.courier_quote_id is None
    assert breakdown.total == Decimal("550.00")


@pytest.mark.asyncio
async def test_unconfigured_courier_base_url_falls_back_to_flat_fee(monkeypatch):
    """
    The exact production scenario at first deploy: an address IS resolved
    (origin/destination both present) but COURIER_BASE_URL is still empty
    because the real courier doesn't exist yet. Checkout must not break.
    """
    from app.config.settings import settings

    monkeypatch.setattr(settings, "courier_base_url", "", raising=False)
    breakdown = await PricingService.quote(
        _ITEMS,
        delivery_fee=Decimal("350"),
        tax_rate=Decimal("0"),
        origin={"name": "Colombo Main", "post_office": "Colombo"},
        destination={"post_office": "Colombo"},
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
        origin={"name": "Colombo Main"},
        destination={"post_office": "Colombo"},
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
        origin={"name": "Colombo Main"},
        destination={"post_office": "Colombo"},
        payment_method="cod",
    )
    assert breakdown.delivery_fee == Decimal("480.00")
    assert breakdown.courier_quote_id == "qt_test123"
    assert breakdown.total == Decimal("680.00")
