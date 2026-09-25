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
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.models.order import OrderStatus, PaymentStatus
from app.services.order_service import OrderService


def _order(**extra):
    fields = {
        "order_id": "11111111-1111-1111-1111-111111111111",
        "order_number": "SB-0001",
        "user_id": "22222222-2222-2222-2222-222222222222",
        "status": OrderStatus.CONFIRMED.value,
        "payment_status": PaymentStatus.PAID.value,
        "payment_method": "MARX_OTHER",
        "total_amount": Decimal("5000.00"),
        "wallet_deduction": Decimal("0"),
        "shipping_amount": Decimal("0"),
        "courier_quote_amount": None,
        "courier_shipment_id": None,
    }
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


def _booked_cod(**extra):
    return _order(
        status=OrderStatus.READY_FOR_PICKUP.value,
        payment_method="COD",
        payment_status=PaymentStatus.PENDING.value,
        shipping_amount=Decimal("350"),
        **extra,
    )


def test_a_booked_cod_order_takes_the_delivery_charge_from_the_wallet():
    """The owner's rule: a COD customer who cancels after the courier is
    booked pays the delivery charge from their Sribees Wallet. Nothing was
    paid for the order, so nothing comes back from it."""
    order = _booked_cod()
    assert OrderService.cancellation_refund_preview(order) == Decimal("0")
    assert OrderService.cancellation_charge(order, Decimal("1000")) == Decimal("350")
    assert OrderService.cancellation_wallet_charge(order, Decimal("1000")) == Decimal("350")


def test_a_short_wallet_pays_only_what_it_has():
    """Take what is there, never a debt."""
    order = _booked_cod()
    assert OrderService.cancellation_charge(order, Decimal("120")) == Decimal("120")
    assert OrderService.cancellation_charge(order, Decimal("0")) == Decimal("0")
    assert OrderService.cancellation_charge(order, Decimal("-40")) == Decimal("0")


def test_this_orders_cashback_cannot_pay_its_own_fee():
    """Cashback already in the wallet is clawed back in the same cancellation;
    counting it as spare balance would take it twice and leave the wallet
    negative."""
    order = _booked_cod(
        cashback_earned=Decimal("100"),
        cashback_credited_at=datetime(2026, 9, 20, tzinfo=timezone.utc),
    )
    assert OrderService.cancellation_charge(order, Decimal("150")) == Decimal("50")


def test_held_cashback_is_not_in_the_balance_to_deduct():
    """Cashback now waits for the return window, so an undelivered order's
    cashback was never in the wallet. Deducting it anyway would let a customer
    with Rs 150 in their wallet off Rs 100 of a charge they can pay."""
    order = _booked_cod(cashback_earned=Decimal("100"), cashback_credited_at=None)
    assert OrderService.cancellation_charge(order, Decimal("150")) == Decimal("150")


def test_a_cod_order_cancelled_before_booking_costs_nothing():
    order = _order(payment_method="COD", payment_status=PaymentStatus.PENDING.value,
                   shipping_amount=Decimal("350"))
    assert OrderService.cancellation_charge(order, Decimal("1000")) == Decimal("0")


def test_without_a_balance_the_charge_is_capped_at_what_was_paid():
    """Zero balance is what an admin-initiated cancellation passes: the
    old rule, so an admin cancelling a COD order never bills the customer."""
    assert OrderService.cancellation_charge(_booked_cod()) == Decimal("0")
    paid = _order(status=OrderStatus.READY_FOR_PICKUP.value,
                  total_amount=Decimal("200"), shipping_amount=Decimal("350"))
    assert OrderService.cancellation_charge(paid) == Decimal("200")


def test_a_prepaid_order_short_of_the_fee_tops_up_from_the_wallet():
    paid = _order(status=OrderStatus.READY_FOR_PICKUP.value,
                  total_amount=Decimal("200"), shipping_amount=Decimal("350"))
    assert OrderService.cancellation_charge(paid, Decimal("500")) == Decimal("350")
    assert OrderService.cancellation_wallet_charge(paid, Decimal("500")) == Decimal("150")
    # What comes back from the order itself is unchanged.
    assert OrderService.cancellation_refund_preview(paid) == Decimal("0")


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


# --- the endpoint and the guard ---------------------------------------------


def test_the_cancel_route_is_the_cancel_handler():
    """The route decorator once ended up on a helper, so POST /cancel ran a
    function returning an IP address and every cancel from the app failed."""
    from app.main import app

    routes = [
        r for r in app.routes
        if getattr(r, "path", "").endswith("/orders/{order_id}/cancel")
        and "POST" in getattr(r, "methods", set())
    ]
    assert len(routes) == 1
    assert routes[0].endpoint.__name__ == "cancel_order"


@pytest.mark.parametrize("status", ["handed_to_courier", "shipped", "out_for_delivery"])
async def test_the_service_accepts_what_the_button_offers(status, monkeypatch):
    """The button is drawn from customer_cancel_state; cancel_order used to
    keep its own, older list and refuse these, so the red button always
    failed for exactly the parcels it was red for."""

    async def _noop(*args, **kwargs):
        return None

    monkeypatch.setattr(OrderService, "_apply_cancellation_effects", _noop)
    monkeypatch.setattr(OrderService, "_cancel_courier_shipment", _noop)

    class _Db:
        commit = refresh = staticmethod(_noop)

    order = _order(status=status, courier_shipment_id="WB-1", notes=None, cancelled_at=None)
    await OrderService.cancel_order(_Db(), order, cancelled_by="customer")
    assert order.status == OrderStatus.CANCELLED.value


async def test_the_service_still_refuses_a_delivered_order():
    order = _order(status=OrderStatus.DELIVERED.value, notes=None, cancelled_at=None)
    with pytest.raises(ValueError):
        await OrderService.cancel_order(None, order)
