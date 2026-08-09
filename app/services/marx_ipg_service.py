"""
Marx IPG Payment Gateway — HTTP client.

Doc: D:/Marx_IPG_API_Integration_Guide.md (compiled from
https://documentation.ipg.v4.marx.lk/). Hosted-page gateway: create an order,
redirect the shopper to the returned ``payUrl``, they pay and complete 3DS on
the issuing bank's page, Marx redirects them back to our ``returnUrl`` with
``trId``/``merchantRID``. The caller must validate those against its own
records BEFORE calling ``initiate_payment`` (doc section 5.3 — never trust
redirect query parameters blindly, they pass through the shopper's browser).

Auth is a single ``merchant-api-key`` header, no OAuth/token exchange. The doc
gives no retry guidance (unlike the courier contract's section 8), so every
call here is a single attempt; failures surface as ``MarxIPGError`` and the
caller decides what to do (order creation aborts before any DB write; a
confirm-endpoint failure surfaces as a 502 to the client; the reconciliation
job just tries again on its next sweep).
"""
from decimal import Decimal
from typing import Optional

import httpx
from loguru import logger

from app.config.settings import settings


class MarxIPGError(Exception):
    """Raised on a non-200 response, top-level ``status != 0``, or a network failure."""

    def __init__(self, message: str, *, status_code: Optional[int] = None, gateway_code: Optional[str] = None):
        super().__init__(message)
        self.status_code = status_code
        self.gateway_code = gateway_code


class MarxIPGService:
    """Client for the three Marx IPG calls SRIBEESonline needs (doc sections 5.1, 5.4, 5.5)."""

    @staticmethod
    def _headers() -> dict:
        return {"merchant-api-key": settings.marx_ipg_api_key, "Content-Type": "application/json"}

    @staticmethod
    def _parse(path: str, response: httpx.Response) -> dict:
        if response.status_code != 200:
            raise MarxIPGError(
                f"Marx IPG {path} returned HTTP {response.status_code}: {response.text[:300]}",
                status_code=response.status_code,
            )
        try:
            body = response.json()
        except ValueError as exc:
            raise MarxIPGError(f"Marx IPG {path} returned a non-JSON body") from exc
        # Top-level status: 0 = success, 1 = failure (doc section 6.1).
        if body.get("status") != 0:
            raise MarxIPGError(
                f"Marx IPG {path} reported failure: {body.get('message')}",
                gateway_code=body.get("message"),
            )
        return body.get("data") or {}

    @staticmethod
    async def _call(method: str, path: str, *, json: Optional[dict] = None) -> dict:
        url = f"{settings.marx_ipg_base_url.rstrip('/')}{path}"
        try:
            async with httpx.AsyncClient(timeout=settings.marx_ipg_timeout_seconds) as client:
                response = await client.request(method, url, json=json, headers=MarxIPGService._headers())
        except (httpx.TimeoutException, httpx.ConnectError) as exc:
            raise MarxIPGError(f"Marx IPG {method} {path} unreachable: {exc}") from exc
        return MarxIPGService._parse(path, response)

    @staticmethod
    async def create_order(
        *,
        merchant_rid: str,
        amount: Decimal,
        return_url: str,
        customer_mail: str,
        customer_mobile: Optional[str],
        order_summary: str,
        customer_reference: str,
        marx_payment_method: str,
    ) -> dict:
        """POST /api/v4/ipg/orders (5.1). Returns {"pay_url": ..., "tr_id": ...}."""
        body = {
            "merchantRID": merchant_rid,
            "amount": float(amount),
            "validTimeLimit": settings.marx_ipg_valid_time_limit_hours,
            "returnUrl": return_url,
            "customerMail": customer_mail,
            "customerMobile": customer_mobile,
            "orderSummary": order_summary,
            "customerReference": customer_reference,
            "paymentMethod": marx_payment_method,
        }
        data = await MarxIPGService._call("POST", "/api/v4/ipg/orders", json=body)
        logger.info(f"Marx IPG order created: trId={data.get('trId')} merchantRID={merchant_rid}")
        return {"pay_url": data.get("payUrl"), "tr_id": data.get("trId")}

    @staticmethod
    async def initiate_payment(*, tr_id: str, merchant_rid: str) -> dict:
        """
        PUT /api/v4/ipg/orders/{trId} (5.4) — confirms/captures the payment.
        Only call this AFTER validating trId/merchantRID against the order that
        created them (5.3).
        """
        data = await MarxIPGService._call(
            "PUT", f"/api/v4/ipg/orders/{tr_id}", json={"merchantRID": merchant_rid}
        )
        gateway_response = data.get("gatewayResponse") or {}
        response = gateway_response.get("response") or {}
        order = gateway_response.get("order") or {}
        logger.info(
            f"Marx IPG payment initiated: trId={tr_id} merchantRID={merchant_rid} "
            f"summaryResult={data.get('summaryResult')} gatewayCode={response.get('gatewayCode')}"
        )
        return {
            "summary_result": data.get("summaryResult"),
            "gateway_code": response.get("gatewayCode"),
            "order_status": order.get("status"),
            "raw": data,
        }

    @staticmethod
    async def get_order_summary(*, tr_id: str) -> dict:
        """GET /api/v4/ipg/orders/{trId}/summary (5.5) — for reconciliation."""
        data = await MarxIPGService._call("GET", f"/api/v4/ipg/orders/{tr_id}/summary")
        gateway_response = data.get("gatewayResponse") or {}
        response = gateway_response.get("response") or {}
        order = data.get("order") or {}
        return {
            "summary_result": data.get("summaryResult"),
            "gateway_code": response.get("gatewayCode"),
            "order_status": order.get("status"),
            "raw": data,
        }
