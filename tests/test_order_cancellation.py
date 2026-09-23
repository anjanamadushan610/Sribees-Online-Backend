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
        shipping_amount=Decimal("0"),
        courier_quote_amount=None,
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


def test_a_booked_parcel_can_still_be_cancelled_but_not_for_free():
    """`ready_for_pickup` IS the courier booking. The customer may still
    change their mind -- no rider has the parcel yet -- but the delivery has
    been arranged and paid for, so the charge does not come back."""
    order = _order(status=OrderStatus.READY_FOR_PICKUP.value)
    assert OrderService.customer_cancel_state(order) == OrderService.CANCEL_WITH_FEE
    assert OrderService.customer_can_cancel(order) is True


@pytest.mark.parametrize(
    "status", ["handed_to_courier", "shipped", "out_for_delivery"],
)
def test_a_parcel_on_a_motorbike_can_still_be_stopped(status):
    """The courier cannot mark it cancelled -- the goods are in somebody's bag
    -- so we raise a REQUEST their rider acts on. The customer's side settles
    either way: they paid for goods they are not receiving."""
    order = _order(status=status, courier_shipment_id="WB-1")
    assert OrderService.customer_cancel_state(order) == OrderService.CANCEL_WITH_FEE
    assert OrderService._rider_already_has_it(order) is True


@pytest.mark.parametrize(
    "status",
    ["delivered", "cancelled", "refunded", "return_requested",
     "return_approved", "delivery_failed", "rto_initiated"],
)
def test_past_the_doorstep_it_is_a_return_not_a_cancellation(status):
    """The goods have changed hands. That is different machinery, with its own
    window, its own waybill and its own fee."""
    order = _order(status=status)
    assert OrderService.customer_cancel_state(order) == OrderService.CANCEL_BLOCKED
    assert OrderService.customer_can_cancel(order) is False


def test_a_parcel_not_yet_collected_is_cancelled_outright_not_requested():
    """Before pickup the courier can simply cancel the booking -- no rider is
    carrying anything, so there is nobody to ask."""
    order = _order(status=OrderStatus.READY_FOR_PICKUP.value, courier_shipment_id="WB-1")
    assert OrderService._rider_already_has_it(order) is False


def test_the_goods_money_still_comes_back_from_a_rider_stage_cancellation():
    """Only the delivery charge is kept. The customer paid for goods they are
    not going to receive."""
    order = _order(
        status=OrderStatus.OUT_FOR_DELIVERY.value,
        courier_shipment_id="WB-1",
        total_amount=Decimal("5000"),
        shipping_amount=Decimal("350"),
    )
    assert OrderService.cancellation_refund_preview(order) == Decimal("4650")


def test_a_shipment_id_charges_the_fee_whatever_the_status_says():
    """The booking is what matters, and a status can be moved by hand. An
    order carrying a waybill has been booked, whatever its status word."""
    order = _order(status=OrderStatus.CONFIRMED.value, courier_shipment_id="WB-99")
    assert OrderService.customer_cancel_state(order) == OrderService.CANCEL_WITH_FEE


# --- the delivery charge, once a parcel is booked ---------------------------


def test_nothing_is_kept_back_before_a_booking():
    """We have spent nothing on this parcel yet."""
    order = _order(shipping_amount=Decimal("350"))
    assert OrderService.cancellation_courier_fee(order) == Decimal("0")
    assert OrderService.cancellation_refund_preview(order) == Decimal("5000")


def test_the_delivery_charge_is_kept_back_once_booked():
    order = _order(
        status=OrderStatus.READY_FOR_PICKUP.value,
        shipping_amount=Decimal("350"),
    )
    assert OrderService.cancellation_courier_fee(order) == Decimal("350")
    assert OrderService.cancellation_refund_preview(order) == Decimal("4650")


def test_the_fee_is_what_the_customer_was_billed_not_the_requote():
    """shipping_amount is the number on the customer's bill.
    courier_quote_amount is what SribeesExpress charges us, and can be higher
    after a re-quote. Deducting that would take more than the customer ever
    agreed to pay for delivery, over a price change they never saw."""
    order = _order(
        status=OrderStatus.READY_FOR_PICKUP.value,
        shipping_amount=Decimal("350"),
        courier_quote_amount=Decimal("900"),
    )
    assert OrderService.cancellation_courier_fee(order) == Decimal("350")


def test_a_cod_customer_is_not_charged_a_fee_they_never_paid():
    """Nothing was paid, so there is nothing to deduct from. Taking the
    courier's fee out of a wallet balance earned elsewhere would be charging
    them for money they never gave us."""
    order = _order(
        status=OrderStatus.READY_FOR_PICKUP.value,
        payment_method="COD",
        payment_status=PaymentStatus.PENDING.value,
        shipping_amount=Decimal("350"),
    )
    assert OrderService.cancellation_refund_preview(order) == Decimal("0")


def test_the_fee_never_pushes_the_refund_below_zero():
    """A fee larger than everything paid must leave zero, not a debt."""
    order = _order(
        status=OrderStatus.READY_FOR_PICKUP.value,
        total_amount=Decimal("200"),
        shipping_amount=Decimal("350"),
    )
    assert OrderService.cancellation_refund_preview(order) == Decimal("0")


def test_a_wallet_paid_order_still_pays_the_delivery_charge():
    """The fee is charged against everything paid, not just the card portion.
    Shrinking only the gateway refund would let a wallet-paid customer cancel
    a booked parcel for free."""
    order = _order(
        status=OrderStatus.READY_FOR_PICKUP.value,
        total_amount=Decimal("2000"),
        wallet_deduction=Decimal("2000"),
        shipping_amount=Decimal("350"),
    )
    assert OrderService.gateway_paid_amount(order) == Decimal("0")
    assert OrderService.cancellation_refund_preview(order) == Decimal("1650")


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
