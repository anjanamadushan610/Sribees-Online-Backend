"""
Tests for customer-initiated cancellation.

Two things here decide whether money is right, and both are easy to get
wrong in a direction nobody notices until a customer complains:

  * the cut-off -- a cancellation accepted after the courier is booked
    cancels something that exists in SribeesExpress's system, not ours;
  * the refund -- the card portion used to be silently kept, because
    _reverse_order_financials only ever returned the WALLET portion.

DB-free, in the style of the neighbouring order tests.
"""
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.models.order import OrderStatus, PaymentStatus
from app.services.order_service import OrderService


def _order(**extra):
    fields = dict(
        order_id="11111111-1111-1111-1111-111111111111",
        order_number="SB-0001",
        user_id="22222222-2222-2222-2222-222222222222",
        status=OrderStatus.CONFIRMED.value,
        payment_status=PaymentStatus.PAID.value,
        payment_method="MARX_OTHER",
        total_amount=Decimal("5000.00"),
        wallet_deduction=Decimal("0"),
        courier_shipment_id=None,
    )
    fields.update(extra)
    return SimpleNamespace(**fields)


# --- who still owns the decision -------------------------------------------


@pytest.mark.parametrize(
    "status",
    ["pending", "confirmed", "processing", "packing", "packed"],
)
def test_the_customer_can_cancel_while_nothing_has_been_booked(status):
    assert OrderService.customer_can_cancel(_order(status=status)) is True


def test_a_booked_parcel_is_no_longer_the_customers_to_cancel():
    """`ready_for_pickup` IS the courier booking. A rider may already be on the
    way, and the cancellation would be of something in somebody else's
    system."""
    assert OrderService.customer_can_cancel(
        _order(status=OrderStatus.READY_FOR_PICKUP.value)
    ) is False


@pytest.mark.parametrize(
    "status",
    ["handed_to_courier", "shipped", "out_for_delivery", "delivered",
     "cancelled", "refunded", "return_requested"],
)
def test_everything_past_pickup_is_a_support_conversation(status):
    assert OrderService.customer_can_cancel(_order(status=status)) is False


def test_a_shipment_id_closes_the_button_whatever_the_status_says():
    """The booking is what matters, and a status can be moved by hand. An
    order carrying a waybill is booked, full stop."""
    order = _order(status=OrderStatus.CONFIRMED.value, courier_shipment_id="WB-99")
    assert OrderService.customer_can_cancel(order) is False


# --- what comes back --------------------------------------------------------


def test_a_card_payment_is_owed_back_in_full():
    """The gap this closes: the card portion used to be kept silently."""
    assert OrderService.gateway_paid_amount(_order()) == Decimal("5000.00")


def test_the_wallet_portion_is_not_counted_twice():
    """_reverse_order_financials already returns wallet_deduction, so this
    must be the REMAINDER or the customer is refunded more than they paid."""
    order = _order(total_amount=Decimal("5000"), wallet_deduction=Decimal("1500"))
    assert OrderService.gateway_paid_amount(order) == Decimal("3500")
    # The two together are the full total, which is what is owed.
    assert OrderService.gateway_paid_amount(order) + order.wallet_deduction == Decimal("5000")


def test_a_cod_order_refunds_nothing():
    """Nothing was paid: the rider collects at the door. Refunding here would
    credit a wallet out of thin air."""
    order = _order(payment_method="COD", payment_status=PaymentStatus.PENDING.value)
    assert OrderService.gateway_paid_amount(order) == Decimal("0")


def test_a_cod_order_marked_paid_still_refunds_nothing():
    """Defensive: even if something marks a COD order paid, COD means the
    money never came to us through the gateway."""
    order = _order(payment_method="COD", payment_status=PaymentStatus.PAID.value)
    assert OrderService.gateway_paid_amount(order) == Decimal("0")


def test_an_unpaid_gateway_order_refunds_nothing():
    """Started a card payment and abandoned it. There is nothing to return."""
    order = _order(payment_status=PaymentStatus.PENDING.value)
    assert OrderService.gateway_paid_amount(order) == Decimal("0")


def test_a_wallet_larger_than_the_total_never_goes_negative():
    """A credit of a negative amount would DEBIT the customer on a
    cancellation -- taking money for cancelling."""
    order = _order(total_amount=Decimal("1000"), wallet_deduction=Decimal("1500"))
    assert OrderService.gateway_paid_amount(order) == Decimal("0")


def test_a_fully_wallet_paid_order_owes_nothing_extra():
    order = _order(total_amount=Decimal("2000"), wallet_deduction=Decimal("2000"))
    assert OrderService.gateway_paid_amount(order) == Decimal("0")
