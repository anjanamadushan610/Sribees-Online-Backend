"""When the app shows a customer their handover code: paid in advance, and
until the rider takes it."""
from types import SimpleNamespace

import pytest

from app.services.delivery_verification_service import show_code_to_customer


def _order(**kw):
    base = {"payment_method": "MARX_OTHER", "payment_status": "paid", "status": "confirmed",
            "delivery_verification_code": "4821", "delivery_verification_status": "pending"}
    base.update(kw)
    return SimpleNamespace(**base)


@pytest.mark.parametrize("status", ["confirmed", "packing", "packed", "ready_for_pickup",
                                    "handed_to_courier", "out_for_delivery", "delivery_failed"])
def test_shown_from_payment_until_handover(status):
    assert show_code_to_customer(_order(status=status))


@pytest.mark.parametrize(
    "change",
    [
        {"payment_method": "COD"},                       # cash is its own proof
        {"payment_status": "pending"},                   # not paid yet
        {"delivery_verification_status": "confirmed"},   # the rider took it
        {"delivery_verification_code": None},
        {"status": "delivered"},
        {"status": "cancelled"},
        {"status": "awaiting_payment", "payment_status": "pending"},
    ],
)
def test_hidden_otherwise(change):
    assert not show_code_to_customer(_order(**change))
