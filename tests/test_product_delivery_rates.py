from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest

from app.services import delivery_quote_service as service
from app.services.pricing_service import DELIVERY_QUOTED, DeliveryQuote


@pytest.mark.asyncio
async def test_fixed_lines_replace_courier_pricing_and_keep_full_booking_quote(monkeypatch):
    rice_id = uuid4()
    db = AsyncMock()
    db.execute.return_value = Mock()
    db.execute.return_value.all.return_value = [(rice_id, Decimal("100.00"))]
    rice = {"product_id": str(rice_id), "weight_kg": Decimal("1.5"), "quantity": 2}
    ordinary = {"product_id": str(uuid4()), "weight_kg": Decimal("0.5"), "quantity": 1}
    items = [rice, ordinary]
    full_quote = DeliveryQuote(DELIVERY_QUOTED, Decimal("250.00"), quote_id="booking")
    subtotal_quote = DeliveryQuote(DELIVERY_QUOTED, Decimal("200.00"), quote_id="ordinary-only")
    quote = AsyncMock(return_value=subtotal_quote)
    monkeypatch.setattr(service.PricingService, "quote_delivery", quote)

    remainder, fixed = await service.split_delivery_items(db, items)
    assert fixed == Decimal("300.00")
    eligible = await service.ordinary_delivery_quote(remainder, items, full_quote, postal_city_id=123)
    quote.assert_awaited_once_with([ordinary], postal_city_id=123)
    assert fixed + eligible.fee == Decimal("500.00")
    assert full_quote.quote_id == "booking"
    assert (await service.ordinary_delivery_quote([], [rice], full_quote)).fee == 0
    assert await service.ordinary_delivery_quote([ordinary], [ordinary], full_quote) is full_quote


def test_partial_waiver_reports_savings_without_labeling_fee_as_free_on_existing_apps():
    quote = DeliveryQuote(
        DELIVERY_QUOTED, Decimal("300.00"),
        original_fee=Decimal("500.00"), promotion="free_delivery",
    )
    destination = SimpleNamespace(address=None, postal_city=None)
    payload = service.delivery_payload(quote, destination)
    assert payload["fee"] == 300
    assert payload["savings"] == 200
    assert payload["original_fee"] is None
