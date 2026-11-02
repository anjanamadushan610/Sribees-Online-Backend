"""Free delivery: who it covers, when it ends, and that the courier quote stays whole."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.services import free_delivery_service as service
from app.services.pricing_service import DELIVERY_QUOTED, DeliveryQuote

NOW = datetime.now(timezone.utc)
QUOTE = DeliveryQuote(status=DELIVERY_QUOTED, fee=Decimal("350.00"), quote_id="q1")


def _offer(enabled=True, months=3, min_order=0, enabled_at=NOW):
    return SimpleNamespace(enabled=enabled, months=months, min_order=Decimal(str(min_order)), enabled_at=enabled_at)


def _db(offer=None, grant=None):
    db = AsyncMock()
    db.get.return_value = offer
    db.scalar.return_value = grant
    return db


def test_months_land_on_a_real_day():
    assert service.add_months(datetime(2026, 11, 30, tzinfo=timezone.utc), 3).date().isoformat() == "2027-02-28"
    assert service.add_months(datetime(2026, 11, 1, tzinfo=timezone.utc), 2).date().isoformat() == "2027-01-01"


@pytest.mark.asyncio
async def test_everyone_in_the_branch_is_covered_while_it_runs():
    assert await service.covering_source(_db(_offer()), uuid4(), uuid4(), Decimal("1")) == service.FROM_BRANCH


@pytest.mark.asyncio
async def test_the_period_counts_from_switching_on():
    gone = _offer(months=1, enabled_at=NOW - timedelta(days=40))
    assert await service.live_offer(_db(gone), uuid4()) is None
    running = _offer(months=3, enabled_at=NOW - timedelta(days=40))
    assert await service.live_offer(_db(running), uuid4()) is running


@pytest.mark.asyncio
async def test_switched_off_covers_nobody():
    assert await service.covering_source(_db(_offer(enabled=False)), uuid4(), uuid4(), Decimal("9999")) is None


@pytest.mark.asyncio
async def test_a_small_order_pays_delivery():
    db = _db(_offer(min_order=2000))
    assert await service.covering_source(db, uuid4(), uuid4(), Decimal("1999.99")) is None
    assert await service.covering_source(db, uuid4(), uuid4(), Decimal("2000")) == service.FROM_BRANCH


@pytest.mark.asyncio
async def test_a_chosen_customer_is_covered_without_a_branch_offer():
    db = _db(None, SimpleNamespace(grant_id=uuid4()))
    assert await service.covering_source(db, uuid4(), uuid4(), Decimal("1")) == service.FROM_GRANT


@pytest.mark.asyncio
async def test_the_charge_is_waived_and_the_courier_price_kept():
    out = await service.apply_to_quote(_db(_offer()), uuid4(), uuid4(), Decimal("500"), QUOTE)
    assert out.fee == 0 and out.original_fee == Decimal("350.00")
    assert out.promotion == service.PROMOTION and out.quote_id == "q1"


@pytest.mark.asyncio
async def test_a_guest_or_an_unpriced_delivery_is_left_alone():
    assert await service.apply_to_quote(_db(_offer()), None, uuid4(), Decimal("500"), QUOTE) is None
    none = DeliveryQuote(status="unavailable")
    assert await service.apply_to_quote(_db(_offer()), uuid4(), uuid4(), Decimal("500"), none) is None


@pytest.mark.asyncio
async def test_the_popup_tells_a_guest_to_make_an_account():
    body = await service.welcome_offer(_db(_offer()), None, uuid4())
    assert body["months"] == 3 and body["needs_account"] is True
    assert (await service.welcome_offer(_db(_offer()), uuid4(), uuid4()))["needs_account"] is False
    assert await service.welcome_offer(_db(None), None, uuid4()) is None
