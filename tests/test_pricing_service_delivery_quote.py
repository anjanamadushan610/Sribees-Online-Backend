"""
PricingService's delivery half: the delivery charge is SribeesExpress's quote,
and when there is no quote there is no charge — and no order.

These replace the old flat-fee fallback tests. Every case that used to quietly
price delivery at an administrator's flat fee now says why there is no price,
so checkout can refuse honestly instead of charging a guess. No live courier is
needed for any of them.
"""
from decimal import Decimal

import pytest

from app.services import courier_credentials
from app.services.courier_service import (
    CourierError,
    CourierQuote,
    CourierUnavailableError,
)
from app.services.pricing_service import (
    DELIVERY_EMPTY_CART,
    DELIVERY_NO_ADDRESS,
    DELIVERY_NOT_SERVICEABLE,
    DELIVERY_QUOTED,
    DELIVERY_UNAVAILABLE,
    DeliveryQuote,
    PricingService,
)

_ITEMS = [{"product_id": "p1", "price": 100, "quantity": 2, "weight_kg": Decimal("1.25")}]
_POSTAL_CITY_ID = 812
_TARGET = courier_credentials.CourierTarget(
    base_url="https://courier.test", api_key="sk_test_branch", environment="test", key_source="branch"
)


def _fake_rate(monkeypatch, quote=None, exc=None, captured=None):
    from app.services import courier_service

    async def _get_rate(**kwargs):
        if captured is not None:
            captured.update(kwargs)
        if exc is not None:
            raise exc
        return quote

    monkeypatch.setattr(courier_service.CourierService, "get_rate", staticmethod(_get_rate))


@pytest.mark.asyncio
async def test_empty_cart_has_zero_delivery_and_asks_nobody(monkeypatch):
    _fake_rate(monkeypatch, exc=AssertionError("the courier must not be called for an empty cart"))
    breakdown = await PricingService.quote([], postal_city_id=_POSTAL_CITY_ID)
    assert breakdown.delivery_status == DELIVERY_EMPTY_CART
    assert breakdown.delivery_fee == Decimal("0.00")
    assert breakdown.courier_quote_id is None


@pytest.mark.asyncio
async def test_no_address_has_no_delivery_charge_rather_than_a_flat_fee(monkeypatch):
    _fake_rate(monkeypatch, exc=AssertionError("no address, nothing to quote"))
    breakdown = await PricingService.quote(_ITEMS, tax_rate=Decimal("0"))
    assert breakdown.delivery_status == DELIVERY_NO_ADDRESS
    assert breakdown.delivery_available is False
    assert breakdown.delivery_fee == Decimal("0.00")
    # Goods only: no invented delivery charge is added to what the customer sees.
    assert breakdown.total == Decimal("200.00")


@pytest.mark.asyncio
async def test_no_courier_credentials_means_unavailable_not_a_price():
    """
    The first-deploy scenario: an address is quotable but nobody has entered a
    SribeesExpress key, so there is no credential scope. That is the same answer
    to a shopper as the courier being down — and it must not look like a price.
    """
    breakdown = await PricingService.quote(_ITEMS, tax_rate=Decimal("0"), postal_city_id=_POSTAL_CITY_ID)
    assert breakdown.delivery_status == DELIVERY_UNAVAILABLE
    assert breakdown.delivery_fee == Decimal("0.00")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "exc",
    [CourierUnavailableError("simulated outage"), CourierError("unauthorized", "Invalid X-Api-Key", 401)],
)
async def test_a_courier_failure_is_unavailable(monkeypatch, exc):
    _fake_rate(monkeypatch, exc=exc)
    async with courier_credentials.using(_TARGET):
        breakdown = await PricingService.quote(_ITEMS, tax_rate=Decimal("0"), postal_city_id=_POSTAL_CITY_ID)
    assert breakdown.delivery_status == DELIVERY_UNAVAILABLE
    assert breakdown.courier_quote_id is None


@pytest.mark.asyncio
async def test_an_unserviceable_postal_city_is_its_own_answer(monkeypatch):
    _fake_rate(
        monkeypatch,
        quote=CourierQuote(
            quote_id=None, amount_lkr=Decimal("0"), serviceable=False, reason="No delivery to this city."
        ),
    )
    async with courier_credentials.using(_TARGET):
        breakdown = await PricingService.quote(_ITEMS, tax_rate=Decimal("0"), postal_city_id=_POSTAL_CITY_ID)
    assert breakdown.delivery_status == DELIVERY_NOT_SERVICEABLE
    assert breakdown.as_dict()["delivery_message"] == "We don't deliver to this postal city yet."


@pytest.mark.asyncio
async def test_the_courier_quote_is_the_delivery_charge(monkeypatch):
    _fake_rate(
        monkeypatch, quote=CourierQuote(quote_id="qt_test123", amount_lkr=Decimal("480.00"), serviceable=True)
    )
    async with courier_credentials.using(_TARGET):
        breakdown = await PricingService.quote(_ITEMS, tax_rate=Decimal("0"), postal_city_id=_POSTAL_CITY_ID)
    assert breakdown.delivery_status == DELIVERY_QUOTED
    assert breakdown.delivery_fee == Decimal("480.00")
    assert breakdown.courier_quote_id == "qt_test123"
    assert breakdown.total == Decimal("680.00")
    body = breakdown.as_dict()
    assert body["delivery_available"] is True
    assert body["delivery_message"] is None


@pytest.mark.asyncio
async def test_the_quote_request_carries_postal_city_weight_cod_and_outlet(monkeypatch):
    captured: dict = {}
    _fake_rate(
        monkeypatch,
        quote=CourierQuote(quote_id="qt_x", amount_lkr=Decimal("400.00"), serviceable=True),
        captured=captured,
    )
    async with courier_credentials.using(_TARGET):
        await PricingService.quote(
            _ITEMS, tax_rate=Decimal("0"), postal_city_id=_POSTAL_CITY_ID, payment_method="cod", outlet_id=7
        )
    assert captured["postal_city_id"] == _POSTAL_CITY_ID
    # 1.25 kg x 2
    assert captured["weight_kg"] == Decimal("2.50")
    assert captured["payment_method"] == "cod"
    # COD carries the pre-delivery payable: 2 x 100, no discount, no tax.
    assert captured["cod_amount"] == Decimal("200")
    assert captured["outlet_id"] == 7


@pytest.mark.asyncio
async def test_prepaid_sends_zero_cod_amount(monkeypatch):
    captured: dict = {}
    _fake_rate(
        monkeypatch,
        quote=CourierQuote(quote_id="qt_x", amount_lkr=Decimal("400.00"), serviceable=True),
        captured=captured,
    )
    async with courier_credentials.using(_TARGET):
        await PricingService.quote(
            _ITEMS, tax_rate=Decimal("0"), postal_city_id=_POSTAL_CITY_ID, payment_method="prepaid"
        )
    assert captured["cod_amount"] == Decimal("0")


@pytest.mark.asyncio
async def test_a_delivery_quote_already_held_is_used_without_asking_again(monkeypatch):
    """The cart passes its cached quote in; pricing must not spend another courier call."""
    _fake_rate(monkeypatch, exc=AssertionError("the held quote must be used"))
    breakdown = await PricingService.quote(
        _ITEMS,
        tax_rate=Decimal("0"),
        delivery=DeliveryQuote(status=DELIVERY_QUOTED, fee=Decimal("350.00")),
    )
    assert breakdown.delivery_fee == Decimal("350.00")
    assert breakdown.total == Decimal("550.00")
