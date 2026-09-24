"""
Tests for the post-delivery return window and the approve/reject contract
(SribeesExpress/backend-fastapi/docs/RETURNS_ARCHITECTURE.md §7).

DB-free, in this repo's existing style: the logic worth pinning here is the
clock arithmetic, the guards, and — the reason this file exists — the fact
that the admin router and OrderService agree on what approving a return even
looks like. They did not, and every approval raised TypeError.
"""
import inspect
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.models.order import OrderStatus
from app.services.order_service import RETURN_WINDOW, OrderService


class _FakeSession:
    """Enough AsyncSession for the code paths under test, which only commit
    and refresh. Records commits so a test can assert work was persisted."""

    def __init__(self) -> None:
        self.commits = 0

    async def commit(self) -> None:
        self.commits += 1

    async def refresh(self, _obj, attribute_names=None) -> None:
        return None

    def add(self, _obj) -> None:
        return None


def _order(*, delivered_at=None, status=OrderStatus.DELIVERED.value, **extra):
    fields = dict(
        order_id="11111111-1111-1111-1111-111111111111",
        order_number="SB-0001",
        user_id="22222222-2222-2222-2222-222222222222",
        status=status,
        delivered_at=delivered_at,
        items=[],
        return_items=None,
        return_reason=None,
        return_comments=None,
        return_requested_at=None,
        return_resolution=None,
        return_resolution_note=None,
        return_approved_at=None,
        return_received_at=None,
        refund_amount=None,
        refunded_at=None,
        delivery_address=None,
    )
    # `extra` overrides rather than duplicates — several tests set a field the
    # defaults above already name.
    fields.update(extra)
    fields["status"] = status
    fields["delivered_at"] = delivered_at
    return SimpleNamespace(**fields)


# --- the window ----------------------------------------------------------------------


def test_the_window_is_twelve_hours():
    assert RETURN_WINDOW == timedelta(hours=12)


def test_expiry_is_measured_from_delivery_not_from_the_last_edit():
    """`updated_at` moves whenever anything touches the row — a COD
    remittance, a support note. Measuring from it would silently reopen a
    closed window."""
    delivered = datetime(2026, 9, 20, 14, 0, tzinfo=timezone.utc)
    order = _order(delivered_at=delivered)
    assert OrderService.return_window_expires_at(order) == delivered + timedelta(hours=12)


def test_a_naive_delivered_at_is_read_as_utc_rather_than_crashing():
    # Rows written before timezone-aware timestamps were used still have to
    # produce an answer — and comparing naive to aware raises TypeError.
    order = _order(delivered_at=datetime(2026, 9, 20, 14, 0))
    assert OrderService.return_window_expires_at(order) == datetime(
        2026, 9, 21, 2, 0, tzinfo=timezone.utc
    )


def test_an_undelivered_order_has_no_window_at_all():
    assert OrderService.return_window_expires_at(_order()) is None


# --- requesting -----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_return_inside_the_window_is_accepted():
    order = _order(delivered_at=datetime.now(timezone.utc) - timedelta(hours=11, minutes=59))
    db = _FakeSession()

    await OrderService.request_return(db, order, "Item not as described", items=None)

    assert order.status == OrderStatus.RETURN_REQUESTED.value
    assert order.return_reason == "Item not as described"
    assert db.commits == 1


@pytest.mark.asyncio
async def test_a_return_after_twelve_hours_is_refused():
    order = _order(delivered_at=datetime.now(timezone.utc) - timedelta(hours=12, minutes=1))
    db = _FakeSession()

    with pytest.raises(ValueError, match="12-hour return window"):
        await OrderService.request_return(db, order, "Item not as described")

    # And nothing was written — a refused request must not half-open a claim.
    assert order.status == OrderStatus.DELIVERED.value
    assert db.commits == 0


@pytest.mark.asyncio
async def test_an_order_with_no_delivery_time_is_refused_rather_than_waved_through():
    """Defaulting to "allowed" here would make every order that predates
    delivered_at being recorded returnable forever."""
    order = _order(delivered_at=None)

    with pytest.raises(ValueError, match="no recorded delivery time"):
        await OrderService.request_return(_FakeSession(), order, "Item not as described")


@pytest.mark.asyncio
async def test_an_undelivered_order_is_refused_before_the_clock_is_even_consulted():
    order = _order(status=OrderStatus.OUT_FOR_DELIVERY.value)

    with pytest.raises(ValueError, match="Only delivered orders"):
        await OrderService.request_return(_FakeSession(), order, "Changed my mind")


# --- the approve/reject contract ------------------------------------------------------
#
# Regression guard. `admin_orders.py` has called these with `resolution=` and
# `note=` since the B4 return-resolution work shipped, against signatures that
# took neither — so every approval and every rejection raised TypeError at
# runtime, and the `return_resolution` column was never once written.


def test_approve_return_accepts_the_arguments_the_router_actually_sends():
    params = inspect.signature(OrderService.approve_return).parameters
    assert "resolution" in params
    assert "note" in params


def test_reject_return_accepts_the_note_the_router_actually_sends():
    assert "note" in inspect.signature(OrderService.reject_return).parameters


@pytest.mark.asyncio
async def test_a_returnless_refund_pays_out_immediately(monkeypatch):
    """Nothing is being collected, so there is nothing to wait for."""
    credited = {}

    async def _fake_credit(db, order):
        credited["called"] = True
        order.refunded_at = datetime.now(timezone.utc)
        order.status = OrderStatus.REFUNDED.value

    monkeypatch.setattr(OrderService, "_credit_return_refund", _fake_credit)
    order = _order(status=OrderStatus.RETURN_REQUESTED.value)

    await OrderService.approve_return(_FakeSession(), order, resolution="returnless_refund")

    assert credited.get("called") is True
    assert order.status == OrderStatus.REFUNDED.value
    assert order.return_resolution == "returnless_refund"


@pytest.mark.asyncio
async def test_a_reverse_pickup_does_not_pay_out_until_the_goods_are_back(monkeypatch):
    """The hole this closes: approval used to credit the wallet and nothing
    ever checked that anything came back."""
    calls = {"credited": False, "booked": False}

    async def _fake_credit(db, order):  # pragma: no cover - asserted not called
        calls["credited"] = True

    async def _fake_book(db, order):
        calls["booked"] = True

    monkeypatch.setattr(OrderService, "_credit_return_refund", _fake_credit)
    monkeypatch.setattr(OrderService, "_book_reverse_pickup", _fake_book)
    monkeypatch.setattr(OrderService, "_compute_return_refund", lambda order: Decimal("4500.00"))
    order = _order(status=OrderStatus.RETURN_REQUESTED.value)

    await OrderService.approve_return(_FakeSession(), order, resolution="reverse_pickup")

    assert order.status == OrderStatus.RETURN_APPROVED.value
    assert calls["booked"] is True
    assert calls["credited"] is False
    # The amount is fixed at approval, so a later catalogue price change
    # cannot move what the customer was promised.
    assert order.refund_amount == Decimal("4500.00")
    assert order.refunded_at is None


@pytest.mark.asyncio
async def test_the_goods_arriving_back_is_what_releases_the_refund(monkeypatch):
    async def _fake_credit(db, order):
        order.refunded_at = datetime.now(timezone.utc)
        order.status = OrderStatus.REFUNDED.value

    monkeypatch.setattr(OrderService, "_credit_return_refund", _fake_credit)
    order = _order(status=OrderStatus.RETURN_APPROVED.value, refund_amount=Decimal("4500.00"))

    await OrderService.complete_return(_FakeSession(), order)

    assert order.status == OrderStatus.REFUNDED.value
    assert order.return_received_at is not None


@pytest.mark.asyncio
async def test_a_replayed_arrival_webhook_cannot_refund_twice(monkeypatch):
    """Webhooks are redelivered. A second `delivered` on the return leg must
    not credit the wallet again."""
    credits = []

    async def _fake_apply(db, **kwargs):
        credits.append(kwargs.get("amount"))

    from app.services.order_service import WalletService

    monkeypatch.setattr(WalletService, "apply_transaction", _fake_apply)
    order = _order(status=OrderStatus.RETURN_APPROVED.value, refund_amount=Decimal("4500.00"))
    db = _FakeSession()

    await OrderService.complete_return(db, order)
    first_refunded_at = order.refunded_at
    await OrderService.complete_return(db, order)

    assert len(credits) == 1
    assert order.refunded_at == first_refunded_at


@pytest.mark.asyncio
async def test_an_unknown_resolution_is_refused():
    order = _order(status=OrderStatus.RETURN_REQUESTED.value)
    with pytest.raises(ValueError, match="Unknown return resolution"):
        await OrderService.approve_return(_FakeSession(), order, resolution="free_money")


@pytest.mark.asyncio
async def test_approving_something_that_is_not_an_open_claim_is_refused():
    order = _order(status=OrderStatus.DELIVERED.value)
    with pytest.raises(ValueError, match="no pending return request"):
        await OrderService.approve_return(_FakeSession(), order)
