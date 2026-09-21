"""Approving a reverse pickup must reach SribeesExpress with credentials.

Every call to them runs inside a credentials scope, because there is no single
correct account to use: a branch with its own SribeesExpress key is billed on
it, and one on the shared account is not. `CourierService._request` therefore
refuses before it builds a request when no scope is active.

`_book_reverse_pickup` shipped without one. A Branch Manager approving a return
got "No SribeesExpress credentials are in scope for this call", with a "Try
booking again" button that could never work — the claim was approved, the
refund was waiting on goods, and no rider was ever sent.

The property is tested at the real call site rather than by reading the source:
the scope is a ContextVar, so it can legitimately be opened by a *caller* one
or more frames up, and no static check can see that.
"""
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.models.order import OrderStatus
from app.services import courier_credentials
from app.services.courier_credentials import CourierNotConfiguredError, CourierTarget
from app.services.courier_service import CourierService
from app.services.order_service import OrderService


class _FakeSession:
    def __init__(self) -> None:
        self.commits = 0

    async def commit(self) -> None:
        self.commits += 1

    async def refresh(self, _obj) -> None:
        return None

    def add(self, _obj) -> None:
        return None


def _approved_return_order():
    return SimpleNamespace(
        order_id=uuid4(),
        order_number="SB-4821",
        user_id=uuid4(),
        branch_id=uuid4(),
        status=OrderStatus.RETURN_APPROVED.value,
        return_reason="Item not as described",
        return_items=None,
        items=[],
        delivery_address=SimpleNamespace(recipient_name="Kamala Perera", phone="0771112222"),
        return_waybill_number=None,
        return_handover_code=None,
        return_fee=None,
        return_tracking_status=None,
        return_booking_status="not_required",
        return_booking_error=None,
    )


@pytest.mark.asyncio
async def test_the_booking_runs_with_credentials_in_scope(monkeypatch):
    """The regression. Before the fix `active_target()` was None here and the
    client refused, so the assertion inside this stub is the whole test."""
    seen: dict = {}

    async def _fake_resolve(db, branch_id=None):
        seen["resolved_branch"] = branch_id
        return CourierTarget(
            base_url="https://api.example.test",
            api_key="sk_live_test",
            environment="live",
            key_source="branch",
        )

    async def _fake_create_return(**kwargs):
        # This is the assertion that matters: by the time the client is
        # called, a target is in scope and it knows whose account to bill.
        assert courier_credentials.active_target() is not None
        seen["kwargs"] = kwargs
        return {
            "return_id": "SXR0000004821",
            "handover_code": "482913",
            "return_fee_lkr": "120.00",
            "status": "booked",
        }

    monkeypatch.setattr(courier_credentials, "resolve", _fake_resolve)
    monkeypatch.setattr(CourierService, "create_return", _fake_create_return)

    order = _approved_return_order()
    db = _FakeSession()
    await OrderService._book_reverse_pickup(db, order)

    assert order.return_booking_status == "booked"
    assert order.return_waybill_number == "SXR0000004821"
    # Shown exactly once by SribeesExpress, who keep only a hash — if this
    # write is lost nobody can recover it.
    assert order.return_handover_code == "482913"
    assert order.return_fee == Decimal("120.00")
    assert order.return_booking_error is None
    # Booked as the fulfilling branch, which is whose key it is billed on.
    assert seen["resolved_branch"] == order.branch_id
    assert seen["kwargs"]["merchant_order_ref"] == "SB-4821"


@pytest.mark.asyncio
async def test_an_unconfigured_branch_says_so_instead_of_offering_a_retry(monkeypatch):
    """"Try booking again" is the wrong answer to "nobody entered a key" — it
    will fail identically every time. The message names the fix instead."""

    async def _fake_resolve(db, branch_id=None):
        raise CourierNotConfiguredError("no key for this branch")

    monkeypatch.setattr(courier_credentials, "resolve", _fake_resolve)

    order = _approved_return_order()
    await OrderService._book_reverse_pickup(_FakeSession(), order)

    assert order.return_booking_status == "failed"
    assert "no SribeesExpress account configured" in order.return_booking_error
    assert "Settings" in order.return_booking_error
    # And nothing half-written: no waybill, no code.
    assert order.return_waybill_number is None
    assert order.return_handover_code is None


@pytest.mark.asyncio
async def test_a_courier_outage_leaves_the_claim_approved_and_retryable(monkeypatch):
    """A manager's decision stands even when SribeesExpress is unreachable —
    the booking is what is retried, not the approval."""
    from app.services.courier_service import CourierError

    async def _fake_resolve(db, branch_id=None):
        return CourierTarget(
            base_url="https://api.example.test",
            api_key="sk_live_test",
            environment="live",
            key_source="account",
        )

    async def _boom(**kwargs):
        raise CourierError("upstream_unavailable", "Gateway timeout", 502)

    monkeypatch.setattr(courier_credentials, "resolve", _fake_resolve)
    monkeypatch.setattr(CourierService, "create_return", _boom)

    order = _approved_return_order()
    await OrderService._book_reverse_pickup(_FakeSession(), order)

    assert order.return_booking_status == "failed"
    assert "upstream_unavailable" in order.return_booking_error
    # Still RETURN_APPROVED — the claim was decided, only the dispatch failed.
    assert order.status == OrderStatus.RETURN_APPROVED.value


@pytest.mark.asyncio
async def test_a_partial_return_sends_the_manifest(monkeypatch):
    """What the rider and our counter staff count against each other."""
    seen: dict = {}
    item_id = uuid4()

    async def _fake_resolve(db, branch_id=None):
        return CourierTarget(
            base_url="https://api.example.test",
            api_key="sk",
            environment="live",
            key_source="account",
        )

    async def _fake_create_return(**kwargs):
        seen["items"] = kwargs.get("items")
        return {"return_id": "SXR1", "handover_code": "111111", "status": "booked"}

    monkeypatch.setattr(courier_credentials, "resolve", _fake_resolve)
    monkeypatch.setattr(CourierService, "create_return", _fake_create_return)

    order = _approved_return_order()
    order.return_items = [{"order_item_id": str(item_id), "quantity": 2}]
    order.items = [
        SimpleNamespace(
            order_item_id=item_id,
            product_sku="SKU-9",
            product_name="Blue kettle",
            quantity=3,
        )
    ]

    await OrderService._book_reverse_pickup(_FakeSession(), order)

    assert seen["items"] == [{"sku": "SKU-9", "name": "Blue kettle", "quantity": 2}]


@pytest.mark.asyncio
async def test_the_customer_is_named_as_the_pickup_contact(monkeypatch):
    """The rider is going to a person's door, not a shop counter."""
    seen: dict = {}

    async def _fake_resolve(db, branch_id=None):
        return CourierTarget(
            base_url="https://api.example.test",
            api_key="sk",
            environment="live",
            key_source="account",
        )

    async def _fake_create_return(**kwargs):
        seen.update(kwargs)
        return {"return_id": "SXR1", "handover_code": "111111", "status": "booked"}

    monkeypatch.setattr(courier_credentials, "resolve", _fake_resolve)
    monkeypatch.setattr(CourierService, "create_return", _fake_create_return)

    await OrderService._book_reverse_pickup(_FakeSession(), _approved_return_order())

    assert seen["pickup_contact_name"] == "Kamala Perera"
    assert seen["pickup_contact_phone"] == "0771112222"
    assert datetime.now(timezone.utc)  # sanity: the test clock is usable
