"""Tests for the extended PaymentMethodEnum / CreateOrderRequest validation."""
import pytest
from pydantic import ValidationError

from app.schemas.order import (
    GATEWAY_PAYMENT_METHODS,
    MARX_PAYMENT_METHOD_MAP,
    CreateOrderRequest,
    to_courier_payment_method,
)

_ADDR = "00000000-0000-0000-0000-000000000000"


@pytest.mark.parametrize(
    "raw,expected",
    [("COD", "COD"), ("wallet", "WALLET"), ("marx_other", "MARX_OTHER"), ("Marx_Amex", "MARX_AMEX")],
)
def test_payment_method_normalizes_case(raw, expected):
    req = CreateOrderRequest(delivery_address_id=_ADDR, payment_method=raw)
    assert req.payment_method == expected


def test_unknown_payment_method_rejected():
    with pytest.raises(ValidationError):
        CreateOrderRequest(delivery_address_id=_ADDR, payment_method="BITCOIN")


def test_gateway_methods_set_matches_marx_map_keys():
    assert GATEWAY_PAYMENT_METHODS == set(MARX_PAYMENT_METHOD_MAP.keys())


def test_marx_payment_method_map_is_lkr_only():
    """LKR-only by design -- no OTHER_USD/AMEX_USD (user decision, see plan)."""
    assert set(MARX_PAYMENT_METHOD_MAP.values()) == {"OTHER", "AMEX", "PAY_BY_BANK_ACCOUNT"}


@pytest.mark.parametrize(
    "method,expected",
    [("COD", "cod"), ("cod", "cod"), ("WALLET", "prepaid"), ("MARX_OTHER", "prepaid"), (None, "prepaid")],
)
def test_to_courier_payment_method(method, expected):
    assert to_courier_payment_method(method) == expected
