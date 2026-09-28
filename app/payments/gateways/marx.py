"""
Marx IPG behind the gateway contract.

The HTTP calls stay in app/services/marx_ipg_service.py; this maps them onto
start / settle / status and decides what Marx's answers mean. Marx's words:
``merchantRID`` is our reference, ``trId`` is theirs, the PUT after the return
captures the payment (doc 5.4), and the summary is for reconciliation (5.5).
"""
from __future__ import annotations

from decimal import Decimal
from typing import Optional

from app.config.settings import settings
from app.payments.gateways.base import (
    Checkout,
    GatewayAnswer,
    GatewayError,
    Outcome,
    Payer,
    PayerDetailsMissing,
    PaymentGateway,
)
from app.services.marx_ipg_service import MarxIPGError, MarxIPGService
from app.utils.identity import normalize_mobile

# A settled payment: "CAPTURED" is what a card capture reports (doc 5.4); the
# order summary says "SUCCESS" (doc 5.5), and a bank-account payment
# (PAY_BY_BANK_ACCOUNT) has no card to capture. Either means the money is in,
# and only together with summaryResult SUCCESS.
_PAID_STATES = ("CAPTURED", "SUCCESS")


def _mobile(phone: Optional[str]) -> Optional[str]:
    """customerMobile as +94771234567.

    The guide's own sample is 94771234567, but production Marx refuses that
    with ``invalid_pattern`` (2026-09-28: every card and bank order failed
    until this went back); +94... is what it has always accepted. Optional,
    so a number we cannot read is left out rather than sent wrong."""
    if not phone:
        return None
    try:
        return normalize_mobile(phone)
    except ValueError:
        return None


def _refusal(exc: MarxIPGError) -> Optional[GatewayAnswer]:
    """Marx answers "not paid yet" and "declined" with top-level status 1 and
    the summary word as the message. Those are answers, not outages."""
    word = (exc.gateway_code or "").upper()
    if word in ("PENDING", "UNKNOWN"):
        return GatewayAnswer(outcome=Outcome.PENDING, code=word)
    if word == "FAILURE":
        return GatewayAnswer(outcome=Outcome.FAILED, code=word)
    return None


def _outcome(summary_result: Optional[str], order_status: Optional[str], paid_states: tuple) -> Outcome:
    if summary_result == "SUCCESS" and order_status in paid_states:
        return Outcome.PAID
    if summary_result == "FAILURE":
        return Outcome.FAILED
    return Outcome.PENDING


class MarxGateway(PaymentGateway):
    name = "marx"

    @property
    def return_url(self) -> str:
        return settings.marx_ipg_return_url

    async def start(
        self,
        *,
        reference: str,
        amount: Decimal,
        option: str,
        payer: Payer,
        description: str,
    ) -> Checkout:
        # Marx requires a real customerMail. Phone-only accounts are asked
        # for one right after OTP; this fires only for an account that
        # predates that screen. Never invent an address for a payment
        # processor.
        if not payer.email:
            raise PayerDetailsMissing(
                "Please add your email in Profile before paying by card or bank transfer."
            )
        try:
            created = await MarxIPGService.create_order(
                merchant_rid=reference,
                amount=amount,
                return_url=self.return_url,
                customer_mail=payer.email,
                customer_mobile=_mobile(payer.phone),
                order_summary=description,
                customer_reference=payer.name or reference,
                marx_payment_method=option,
            )
        except MarxIPGError as exc:
            raise GatewayError(str(exc)) from exc
        if not created.get("pay_url"):
            raise GatewayError("Marx returned no payment page")
        tr_id = created.get("tr_id")
        return Checkout(redirect_url=created["pay_url"], gateway_ref=str(tr_id) if tr_id is not None else None)

    def parse_return(self, params: dict) -> Optional[tuple[str, str]]:
        merchant_rid, tr_id = params.get("merchantRID"), params.get("trId")
        if not merchant_rid or not tr_id:
            return None
        return str(merchant_rid), str(tr_id)

    async def settle(self, *, reference: str, gateway_ref: str) -> GatewayAnswer:
        try:
            result = await MarxIPGService.initiate_payment(tr_id=gateway_ref, merchant_rid=reference)
        except MarxIPGError as exc:
            answer = _refusal(exc)
            if answer is not None:
                return answer
            raise GatewayError(str(exc)) from exc
        return GatewayAnswer(
            outcome=_outcome(result.get("summary_result"), result.get("order_status"), _PAID_STATES),
            code=result.get("gateway_code") or result.get("summary_result"),
            raw=result.get("raw") or {},
        )

    async def status(self, *, reference: str, gateway_ref: str) -> GatewayAnswer:
        try:
            result = await MarxIPGService.get_order_summary(tr_id=gateway_ref)
        except MarxIPGError as exc:
            answer = _refusal(exc)
            if answer is not None:
                return answer
            raise GatewayError(str(exc)) from exc
        return GatewayAnswer(
            outcome=_outcome(result.get("summary_result"), result.get("order_status"), _PAID_STATES),
            code=result.get("gateway_code") or result.get("summary_result"),
            raw=result.get("raw") or {},
        )
