"""
Courier Service — HTTP client for the courier's /v1/* API.

Contract: fastapi_backend/docs/COURIER_API.md. SRIBEESonline is the client;
the courier exposes the API. Every call sends ``X-Api-Key:
settings.courier_api_key``. Per the contract's own reliability requirements
(section 8): a 3s timeout and exactly ONE retry on timeout/5xx (never on 4xx —
a 4xx is a real answer, not an outage). Callers distinguish "unreachable"
(``CourierUnavailableError`` — fall back to the flat delivery fee, per section
8's own "we will not block a sale on a courier outage") from "quote expired"
(``CourierQuoteExpiredError`` — re-quote, this is not an outage).
"""
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Optional

import httpx
from loguru import logger

from app.config.settings import settings


class CourierUnavailableError(Exception):
    """Timeout/5xx after the one allowed retry, or an unexpected non-2xx/404."""


class CourierQuoteExpiredError(Exception):
    """409 quote_expired from POST /v1/shipments (section 6)."""


@dataclass
class CourierQuote:
    """Result of POST /v1/rates (section 4)."""
    quote_id: Optional[str]
    amount_lkr: Decimal
    serviceable: bool
    expires_at: Optional[datetime] = None
    reason: Optional[str] = None


def _parse_datetime(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


class CourierService:
    """Client for the courier's rate/shipment/tracking endpoints."""

    @staticmethod
    def _headers() -> dict:
        return {"X-Api-Key": settings.courier_api_key, "Content-Type": "application/json"}

    @staticmethod
    async def _request(method: str, path: str, *, json: Optional[dict] = None) -> httpx.Response:
        """
        One retry on timeout/connection-error/5xx only (section 8). A 4xx
        response is returned as-is, not retried — callers inspect the status
        code themselves (409 quote_expired, 422, 401, etc).

        An unconfigured ``courier_base_url`` (the real courier doesn't exist
        yet) is treated as unavailable immediately, without attempting a
        network call — an empty/relative base URL would otherwise raise an
        ``httpx.InvalidURL`` that this method's except clause doesn't catch,
        which would break EVERY checkout (not just gateway ones) the moment
        this ships to an environment without courier settings configured.
        """
        if not settings.courier_base_url:
            raise CourierUnavailableError("courier_base_url is not configured")

        url = f"{settings.courier_base_url.rstrip('/')}{path}"
        timeout = settings.courier_request_timeout_seconds
        last_exc: Optional[Exception] = None

        for attempt in range(2):
            try:
                async with httpx.AsyncClient(timeout=timeout) as client:
                    response = await client.request(
                        method, url, json=json, headers=CourierService._headers()
                    )
                if response.status_code >= 500 and attempt == 0:
                    logger.warning(
                        f"Courier {method} {path} returned {response.status_code}, retrying once"
                    )
                    continue
                return response
            except (httpx.TimeoutException, httpx.ConnectError) as exc:
                last_exc = exc
                if attempt == 0:
                    logger.warning(f"Courier {method} {path} timed out/unreachable, retrying once")
                    continue
                break

        raise CourierUnavailableError(f"Courier {method} {path} unreachable: {last_exc}")

    @staticmethod
    async def get_rate(
        *,
        origin: dict,
        destination: dict,
        weight_kg: Decimal,
        item_count: int,
        order_value_lkr: Decimal,
        payment_method: str,
    ) -> CourierQuote:
        """POST /v1/rates (section 4). Raises CourierUnavailableError on failure."""
        payload = {
            "origin": origin,
            "destination": destination,
            "parcel": {"total_weight_kg": float(weight_kg), "item_count": item_count},
            "order_value_lkr": float(order_value_lkr),
            "payment_method": payment_method,
        }
        response = await CourierService._request("POST", "/v1/rates", json=payload)
        if response.status_code != 200:
            raise CourierUnavailableError(
                f"Courier /v1/rates returned {response.status_code}: {response.text[:200]}"
            )
        data = response.json()
        if data.get("serviceable") is False:
            return CourierQuote(
                quote_id=None, amount_lkr=Decimal("0"), serviceable=False, reason=data.get("reason")
            )
        return CourierQuote(
            quote_id=data.get("quote_id"),
            amount_lkr=Decimal(str(data["amount_lkr"])),
            serviceable=True,
            expires_at=_parse_datetime(data.get("expires_at")),
        )

    @staticmethod
    async def book_shipment(
        *,
        order_number: str,
        quote_id: Optional[str],
        origin: dict,
        destination: dict,
        weight_kg: Decimal,
        item_count: int,
        payment_method: str,
        cod_amount_lkr: Decimal,
    ) -> dict:
        """
        POST /v1/shipments (section 5). ``order_number`` is the idempotency key
        — safe to call again after a network-level failure; the courier
        returns the existing shipment with 200 rather than creating a
        duplicate. Raises CourierQuoteExpiredError on 409 quote_expired.
        """
        payload = {
            "quote_id": quote_id,
            "order_number": order_number,
            "origin": origin,
            "destination": destination,
            "parcel": {"total_weight_kg": float(weight_kg), "item_count": item_count},
            "payment_method": payment_method,
            "cod_amount_lkr": float(cod_amount_lkr),
        }
        response = await CourierService._request("POST", "/v1/shipments", json=payload)
        if response.status_code == 409:
            raise CourierQuoteExpiredError(response.text[:200])
        if response.status_code not in (200, 201):
            raise CourierUnavailableError(
                f"Courier /v1/shipments returned {response.status_code}: {response.text[:200]}"
            )
        return response.json()

    @staticmethod
    async def get_shipment(shipment_id: str) -> dict:
        """GET /v1/shipments/{id} (section 10)."""
        response = await CourierService._request("GET", f"/v1/shipments/{shipment_id}")
        if response.status_code != 200:
            raise CourierUnavailableError(
                f"Courier GET /v1/shipments/{shipment_id} returned {response.status_code}"
            )
        return response.json()

    @staticmethod
    async def cancel_shipment(shipment_id: str) -> dict:
        """POST /v1/shipments/{id}/cancel."""
        response = await CourierService._request("POST", f"/v1/shipments/{shipment_id}/cancel")
        if response.status_code not in (200, 204):
            raise CourierUnavailableError(
                f"Courier cancel /v1/shipments/{shipment_id} returned {response.status_code}"
            )
        return response.json() if response.content else {}
