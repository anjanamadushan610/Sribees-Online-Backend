"""
The Marx adapter reads Marx's answers the way the API guide writes them,
for cards and for PAY_BY_BANK_ACCOUNT alike.
"""
from decimal import Decimal

import pytest

from app.payments.gateways.base import Outcome, Payer
from app.payments.gateways.marx import MarxGateway
from app.services.marx_ipg_service import MarxIPGService


@pytest.mark.parametrize(
    ("summary", "order_status", "outcome"),
    [
        ("SUCCESS", "CAPTURED", Outcome.PAID),   # a card, captured (5.4)
        ("SUCCESS", "SUCCESS", Outcome.PAID),    # a bank-account payment
        ("SUCCESS", None, Outcome.PENDING),      # not settled yet: ask again later
        ("FAILURE", None, Outcome.FAILED),
        ("PENDING", None, Outcome.PENDING),
        ("UNKNOWN", None, Outcome.PENDING),
    ],
)
async def test_settle_reads_the_answer_for_cards_and_bank_payments(monkeypatch, summary, order_status, outcome):
    async def initiate(*, tr_id, merchant_rid):
        return {"summary_result": summary, "order_status": order_status, "gateway_code": "X", "raw": {}}

    monkeypatch.setattr(MarxIPGService, "initiate_payment", staticmethod(initiate))
    answer = await MarxGateway().settle(reference="FC-1", gateway_ref="5057")
    assert answer.outcome == outcome


@pytest.mark.parametrize(
    ("phone", "sent"),
    [("+94771234567", "94771234567"), ("0771234567", "94771234567"), ("12", None), (None, None)],
)
async def test_the_mobile_goes_as_the_guide_writes_it(monkeypatch, phone, sent):
    seen = {}

    async def create(**kwargs):
        seen.update(kwargs)
        return {"pay_url": "https://ipg.marx.test/?tr=1", "tr_id": 1}

    monkeypatch.setattr(MarxIPGService, "create_order", staticmethod(create))
    await MarxGateway().start(
        reference="FC-1", amount=Decimal("100"), option="PAY_BY_BANK_ACCOUNT",
        payer=Payer(email="a@b.lk", phone=phone, name="A"), description="x",
    )
    assert seen["customer_mobile"] == sent
    assert seen["marx_payment_method"] == "PAY_BY_BANK_ACCOUNT"
