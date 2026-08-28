"""
Tests for DeliveryVerificationService's pure, DB-free logic: code shape and
eligibility.

generate_for_order()/confirm() need a real Order row + DB session (commit),
so they are integration-tested against a real database, not here (see
tests/README.md and the plan's note on this repo having no test-DB
infrastructure yet).
"""
import pytest

from app.config.settings import settings
from app.schemas.order import PaymentMethodEnum
from app.services.delivery_verification_service import (
    MAX_CODE_DIGITS,
    MIN_CODE_DIGITS,
    DeliveryVerificationService,
    is_code_eligible,
)


class _FakeOrder:
    """Minimal stand-in -- is_code_eligible only reads payment_method."""

    def __init__(self, payment_method: str) -> None:
        self.payment_method = payment_method


def test_generated_code_is_exactly_configured_digit_length():
    code = DeliveryVerificationService._generate_code()
    expected = max(MIN_CODE_DIGITS, min(MAX_CODE_DIGITS, settings.delivery_verification_code_digits))
    assert len(code) == expected
    assert code.isdigit()


def test_generated_code_length_is_clamped_to_what_sribeesexpress_accepts(monkeypatch):
    """
    SribeesExpress accepts 4-6 digits (contract section 7). A misconfigured
    setting must not produce a code that fails at booking time.
    """
    for configured, expected in ((1, MIN_CODE_DIGITS), (3, MIN_CODE_DIGITS), (9, MAX_CODE_DIGITS)):
        monkeypatch.setattr(settings, "delivery_verification_code_digits", configured, raising=False)
        assert len(DeliveryVerificationService._generate_code()) == expected


def test_generated_codes_are_zero_padded_and_vary(monkeypatch):
    monkeypatch.setattr(settings, "delivery_verification_code_digits", 4, raising=False)
    codes = {DeliveryVerificationService._generate_code() for _ in range(200)}
    for code in codes:
        assert len(code) == 4
        assert 0 <= int(code) <= 9999
    # Not a strict randomness proof, just a sanity check that we are not
    # somehow always generating the same code.
    assert len(codes) > 1


def test_prepaid_orders_are_always_code_eligible():
    for method in (
        PaymentMethodEnum.WALLET.value,
        PaymentMethodEnum.MARX_OTHER.value,
        PaymentMethodEnum.MARX_BANK.value,
    ):
        assert is_code_eligible(_FakeOrder(method)) is True


def test_cod_orders_are_not_code_eligible_by_default(monkeypatch):
    """
    SribeesExpress rejects a handover code on a COD shipment today (contract
    section 7). Sending one would 400 the whole booking, so the default must
    stay off -- see docs/SRIBEESEXPRESS_INTEGRATION_GAPS.md section 14.
    """
    monkeypatch.setattr(settings, "courier_handover_code_for_cod", False, raising=False)
    assert is_code_eligible(_FakeOrder(PaymentMethodEnum.COD.value)) is False


def test_cod_becomes_code_eligible_behind_the_flag(monkeypatch):
    """The day SribeesExpress accepts it, one setting is the whole change."""
    monkeypatch.setattr(settings, "courier_handover_code_for_cod", True, raising=False)
    assert is_code_eligible(_FakeOrder(PaymentMethodEnum.COD.value)) is True


@pytest.mark.asyncio
async def test_generate_for_order_is_a_noop_for_ineligible_orders(monkeypatch):
    monkeypatch.setattr(settings, "courier_handover_code_for_cod", False, raising=False)

    class _Order(_FakeOrder):
        delivery_verification_code = None
        delivery_verification_status = "not_required"
        order_number = "FC-20260828-TEST01"

    order = _Order(PaymentMethodEnum.COD.value)
    await DeliveryVerificationService.generate_for_order(None, order)
    assert order.delivery_verification_code is None
    assert order.delivery_verification_status == "not_required"


@pytest.mark.asyncio
async def test_generate_for_order_is_idempotent():
    class _Order(_FakeOrder):
        delivery_verification_code = "1234"
        delivery_verification_status = "pending"
        order_number = "FC-20260828-TEST02"

    order = _Order(PaymentMethodEnum.WALLET.value)
    await DeliveryVerificationService.generate_for_order(None, order)
    assert order.delivery_verification_code == "1234"
