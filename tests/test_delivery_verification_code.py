"""
Tests for DeliveryVerificationService's pure code-generation logic.

generate_for_order()/confirm() need a real Order row + DB session (commit),
so they are integration-tested against a real database, not here (see
tests/README.md and the plan's note on this repo having no test-DB
infrastructure yet). This file covers the DB-free piece: code shape.
"""
from app.config.settings import settings
from app.services.delivery_verification_service import DeliveryVerificationService


def test_generated_code_is_exactly_configured_digit_length():
    code = DeliveryVerificationService._generate_code()
    assert len(code) == settings.delivery_verification_code_digits
    assert code.isdigit()


def test_generated_code_is_zero_padded_and_in_range():
    codes = {DeliveryVerificationService._generate_code() for _ in range(200)}
    for code in codes:
        assert 0 <= int(code) <= 999
        assert len(code) == 3
    # Not a strict randomness proof, just a sanity check that we are not
    # somehow always generating the same code.
    assert len(codes) > 1
