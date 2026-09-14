"""
"Ready for pickup" is the moment an order is booked with SribeesExpress.

What these pin down, DB-free:

* the booking carries what the customer agreed to — the quote confirmed at
  checkout, and how they pay (COD with the order total to collect, or prepaid
  with the handover code);
* the booking is made in the fulfilling branch's credential scope;
* a refused booking, or an unpaid prepaid order, leaves the order where it was
  instead of claiming a rider is on the way;
* an expired hold is re-quoted and the courier's new price recorded beside the
  one the customer paid, not silently absorbed.
"""
from contextlib import asynccontextmanager
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.schemas.order import OrderStatusEnum
from app.services import courier_credentials, delivery_quote_service
from app.services.courier_service import (
    CourierQuote,
    CourierQuoteExpiredError,
    CourierService,
)
from app.services.order_service import OrderService

_BRANCH_ID = uuid4()
_TARGET = courier_credentials.CourierTarget(
    base_url="https://courier.test",
    api_key="sk_test_galle",
    environment="test",
    key_source="branch",
    branch_id=_BRANCH_ID,
)


class _FakeDB:
    def __init__(self, address):
        self._address = address
        self.commits = 0
        self.added = []

    async def get(self, model, key):
        return self._address

    async def commit(self):
        self.commits += 1

    async def refresh(self, obj):
        return None

    def add(self, obj):
        self.added.append(obj)

    async def execute(self, *args, **kwargs):  # pragma: no cover - not reached in these paths
        raise AssertionError("no query expected")


def _order(**overrides):
    base = {
        "order_id": uuid4(),
        "order_number": "FC-20260913-ABC123",
        "branch_id": _BRANCH_ID,
        "status": "packed",
        "payment_method": "COD",
        "payment_status": "pending",
        "total_amount": Decimal("4820.00"),
        "shipping_amount": Decimal("555.00"),
        "courier_quote_id": "qt_agreed",
        "courier_quote_expires_at": None,
        "courier_quote_amount": Decimal("555.00"),
        "courier_shipment_id": None,
        "courier_booking_status": "pending",
        "delivery_address_id": uuid4(),
        "delivery_verification_code": None,
        "delivery_slot_date": None,
        "delivery_slot_time": None,
        "packed_at": None,
        "ready_for_pickup_at": None,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


_ADDRESS = SimpleNamespace(
    recipient_name="A. Perera",
    phone="0771234567",
    address_line1="7 Beach Road",
    address_line2=None,
    postal_city="Galle",
    district="Galle",
    province="Southern Province",
    postal_code="80000",
    courier_postal_city_id=812,
)


@pytest.fixture
def branch_scope(monkeypatch):
    """Record which branch the booking asked credentials for, and provide them."""
    asked: list = []

    @asynccontextmanager
    async def _for_branch(db, branch_id):
        asked.append(branch_id)
        async with courier_credentials.using(_TARGET):
            yield _TARGET

    async def _outlet_for(db, branch_id):
        return None

    monkeypatch.setattr(courier_credentials, "for_branch", _for_branch)
    monkeypatch.setattr(delivery_quote_service, "outlet_for", _outlet_for)
    return asked


@pytest.fixture
def bookings(monkeypatch):
    sent: list[dict] = []

    async def _book_shipment(**kwargs):
        sent.append(kwargs)
        return {"shipment_id": "SXP0000000042", "status": "booked", "handover": {"required": False}}

    monkeypatch.setattr(CourierService, "book_shipment", staticmethod(_book_shipment))
    return sent


@pytest.mark.asyncio
async def test_a_cod_booking_sends_the_agreed_quote_and_the_cash_to_collect(branch_scope, bookings):
    order = _order()
    failure = await OrderService._book_courier_shipment(_FakeDB(_ADDRESS), order)

    assert failure is None
    [booking] = bookings
    assert booking["quote_id"] == "qt_agreed"
    assert booking["payment_method"] == "cod"
    assert booking["cod_amount"] == Decimal("4820.00")
    assert booking["handover_code"] is None
    assert booking["postal_city"] == "Galle"
    assert branch_scope == [_BRANCH_ID]
    assert order.courier_shipment_id == "SXP0000000042"
    assert order.courier_booking_status == "booked"


@pytest.mark.asyncio
async def test_a_prepaid_booking_collects_nothing_and_carries_the_handover_code(branch_scope, bookings):
    order = _order(payment_method="MARX_OTHER", payment_status="paid", delivery_verification_code="4821")
    await OrderService._book_courier_shipment(_FakeDB(_ADDRESS), order)

    [booking] = bookings
    assert booking["payment_method"] == "prepaid"
    assert booking["cod_amount"] == Decimal("0")
    assert booking["handover_code"] == "4821"


@pytest.mark.asyncio
async def test_an_expired_hold_is_requoted_and_the_new_price_recorded(monkeypatch, branch_scope, bookings):
    order = _order()
    attempts = {"n": 0}

    async def _book_shipment(**kwargs):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise CourierQuoteExpiredError("quote_expired", "gone", 409)
        bookings.append(kwargs)
        return {"shipment_id": "SXP0000000043", "status": "booked"}

    async def _fresh(db, order, address, cod_amount, outlet_id):
        return CourierQuote(quote_id="qt_fresh", amount_lkr=Decimal("600.00"), serviceable=True), None

    monkeypatch.setattr(CourierService, "book_shipment", staticmethod(_book_shipment))
    monkeypatch.setattr(OrderService, "_fresh_courier_quote", staticmethod(_fresh))

    assert await OrderService._book_courier_shipment(_FakeDB(_ADDRESS), order) is None
    assert bookings[-1]["quote_id"] == "qt_fresh"
    assert order.courier_quote_amount == Decimal("600.00")
    # What the customer paid is untouched; the difference is visible, not absorbed silently.
    assert order.shipping_amount == Decimal("555.00")


def test_cod_is_never_held_back_for_payment():
    assert OrderService.payment_blocks_dispatch(_order(payment_method="COD", payment_status="pending")) is None


def test_an_unpaid_prepaid_order_is_held_back():
    reason = OrderService.payment_blocks_dispatch(_order(payment_method="MARX_OTHER", payment_status="pending"))
    assert reason and "not been paid" in reason


def test_a_paid_prepaid_order_may_go():
    assert OrderService.payment_blocks_dispatch(_order(payment_method="WALLET", payment_status="paid")) is None


@pytest.mark.asyncio
async def test_a_refused_booking_keeps_the_order_packed(monkeypatch):
    async def _refuse(db, order):
        return "SribeesExpress refused or could not be reached: outlet_deactivated"

    monkeypatch.setattr(OrderService, "_book_courier_shipment", staticmethod(_refuse))
    order = _order()
    db = _FakeDB(_ADDRESS)

    with pytest.raises(ValueError) as exc_info:
        await OrderService.update_status(
            db, order, OrderStatusEnum.READY_FOR_PICKUP, enforce=True, actor_role="branch_manager"
        )
    assert "outlet_deactivated" in str(exc_info.value)
    assert order.status == "packed"
    assert order.ready_for_pickup_at is None
    assert db.added == []  # no status history for a transition that did not happen


@pytest.mark.asyncio
async def test_an_unpaid_prepaid_order_cannot_be_marked_ready(monkeypatch):
    async def _must_not_book(db, order):  # pragma: no cover - the point is it is not called
        raise AssertionError("an unpaid order must not be booked")

    monkeypatch.setattr(OrderService, "_book_courier_shipment", staticmethod(_must_not_book))
    order = _order(payment_method="MARX_BANK", payment_status="pending")

    with pytest.raises(ValueError):
        await OrderService.update_status(
            _FakeDB(_ADDRESS), order, OrderStatusEnum.READY_FOR_PICKUP, enforce=True, actor_role="super_admin"
        )
    assert order.status == "packed"


@pytest.mark.asyncio
async def test_a_successful_booking_marks_the_order_ready(monkeypatch):
    async def _book(db, order):
        order.courier_shipment_id = "SXP0000000044"
        return None

    monkeypatch.setattr(OrderService, "_book_courier_shipment", staticmethod(_book))
    order = _order()
    db = _FakeDB(_ADDRESS)

    await OrderService.update_status(
        db, order, OrderStatusEnum.READY_FOR_PICKUP, enforce=True, actor_role="branch_manager"
    )
    assert order.status == "ready_for_pickup"
    assert order.ready_for_pickup_at is not None
    assert len(db.added) == 1  # the status history row


@pytest.mark.asyncio
async def test_a_role_outside_fulfilment_cannot_trigger_a_booking(monkeypatch):
    async def _must_not_book(db, order):  # pragma: no cover
        raise AssertionError("customer support must not dispatch parcels")

    monkeypatch.setattr(OrderService, "_book_courier_shipment", staticmethod(_must_not_book))
    with pytest.raises(ValueError):
        await OrderService.update_status(
            _FakeDB(_ADDRESS),
            _order(),
            OrderStatusEnum.READY_FOR_PICKUP,
            enforce=True,
            actor_role="customer_support",
        )
