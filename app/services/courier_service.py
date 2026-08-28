"""
Courier Service — HTTP client for SribeesExpress's ``/api/v1/ecommerce/*`` API.

Contract: ``docs/SRIBEESEXPRESS_ECOMMERCE_API.md``. SRIBEESonline is the
client; SribeesExpress exposes the API. Every call sends ``X-Api-Key:
settings.courier_api_key``.

Three things about this contract shape the client:

* **Errors are an envelope**, ``{"error": {"code", "message"}}``, and the
  contract's section 4 guarantees ``code`` is stable across every response in
  the domain — including ``401`` and ``429``. So we branch on ``error.code``,
  never on the status code alone (two distinct ``409``\\s exist:
  ``quote_expired`` and ``quote_already_used``, and they need opposite
  handling).
* **Retry policy**: one retry on timeout/connection-error/5xx, never on 4xx (a
  4xx is a real answer, not an outage). A ``429`` is honoured with a short
  ``Retry-After`` sleep and one retry — anything longer is not worth blocking
  a checkout request for.
* **Availability is not correctness**: callers distinguish "unreachable"
  (``CourierUnavailableError`` — fall back to the flat delivery fee, we never
  block a sale on a courier outage) from a real answer such as
  ``CourierQuoteExpiredError`` (re-quote) or ``serviceable: false`` (a normal
  UI state).
"""
import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional

import httpx
from loguru import logger

from app.config.settings import settings

# The contract's own path prefix. settings.courier_base_url holds the origin
# only, so that a deployment can point at a mock/staging host without also
# having to know the API's internal layout.
API_PREFIX = "/api/v1/ecommerce"

# Max length of the flat recipient_address string (contract section 7).
RECIPIENT_ADDRESS_MAX = 255


class CourierUnavailableError(Exception):
    """Timeout/5xx after the one allowed retry, or an unexpected non-2xx."""


class CourierError(Exception):
    """
    A real, structured answer from SribeesExpress that the caller must handle
    — never an outage. ``code`` is the contract's stable ``error.code``.
    """

    def __init__(self, code: str, message: str, status_code: int) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.status_code = status_code


class CourierQuoteExpiredError(CourierError):
    """``quote_expired`` (409) — the quote aged out; re-quote and re-book."""


class CourierQuoteAlreadyUsedError(CourierError):
    """
    ``quote_already_used`` (409) — this quote already booked a shipment.

    Distinct from an expired quote: retrying with the *same* quote can never
    succeed, so the only recovery is a fresh ``/rates`` call. Sharing a 409
    with ``quote_expired`` is exactly why this client branches on
    ``error.code``.
    """


@dataclass
class CourierQuote:
    """Result of ``POST /ecommerce/rates`` (contract section 6)."""

    quote_id: Optional[str]
    amount_lkr: Decimal
    serviceable: bool
    expires_at: Optional[datetime] = None
    reason: Optional[str] = None


@dataclass
class LocationResolution:
    """
    Result of ``POST /ecommerce/locations/resolve`` (contract section 5a).

    Never an error: an unresolvable location is a normal ``200`` answer, so
    ``resolved=False`` carries a ``reason`` rather than raising.
    """

    resolved: bool
    city_id: Optional[int] = None
    city_name: Optional[str] = None
    reason: Optional[str] = None
    candidates: List[dict] = field(default_factory=list)


def _parse_datetime(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _decimal(value: Any) -> Optional[Decimal]:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (ArithmeticError, ValueError):
        return None


def format_recipient_address(
    address_line1: Optional[str],
    address_line2: Optional[str] = None,
    post_office: Optional[str] = None,
    district: Optional[str] = None,
    postal_code: Optional[str] = None,
) -> str:
    """
    Comma-join our structured address into the contract's single
    ``recipient_address`` string, truncated to its documented 255-char max.

    We also send the structured fields alongside it (they win server-side per
    section 7), so this is the fallback rendering — but it is what a rider
    actually reads if SribeesExpress ever falls back to the flat field, so it
    is built in the order a Sri Lankan address is normally written.
    """
    parts = [p.strip() for p in (address_line1, address_line2, post_office, district, postal_code) if p and p.strip()]
    joined = ", ".join(parts)
    return joined[:RECIPIENT_ADDRESS_MAX]


class CourierService:
    """Client for SribeesExpress's rate/shipment/tracking/pickup endpoints."""

    # ------------------------------------------------------------------
    # Transport
    # ------------------------------------------------------------------

    @staticmethod
    def _headers() -> dict:
        return {"X-Api-Key": settings.courier_api_key, "Content-Type": "application/json"}

    @staticmethod
    def _raise_for_error(response: httpx.Response, path: str) -> None:
        """
        Turn a 4xx into the right exception type using the contract's stable
        ``error.code``. Anything we cannot parse is treated as an outage
        rather than silently swallowed — a malformed error body means we do
        not actually know what happened.
        """
        try:
            payload = response.json()
            error = payload.get("error") or {}
            code = error.get("code")
            message = error.get("message") or ""
        except (ValueError, AttributeError):
            code, message = None, response.text[:200]

        if not code:
            raise CourierUnavailableError(
                f"Courier {path} returned {response.status_code} with an unparseable body: {message}"
            )

        if code == "quote_expired":
            raise CourierQuoteExpiredError(code, message, response.status_code)
        if code == "quote_already_used":
            raise CourierQuoteAlreadyUsedError(code, message, response.status_code)
        raise CourierError(code, message, response.status_code)

    @staticmethod
    async def _request(
        method: str,
        path: str,
        *,
        json: Optional[dict] = None,
        params: Optional[dict] = None,
    ) -> Any:
        """
        Perform one call and return the decoded JSON body.

        One retry on timeout/connection-error/5xx (never on 4xx). A ``429`` is
        retried once after its ``Retry-After``, capped — a courier throttling
        us must not hold a customer's checkout request open indefinitely.

        An unconfigured ``courier_base_url`` is treated as unavailable
        immediately, without attempting a network call: an empty base URL
        would otherwise raise ``httpx.InvalidURL``, which the except clause
        below does not catch, and that would break EVERY checkout rather than
        falling back to the flat delivery fee.
        """
        if not settings.courier_base_url:
            raise CourierUnavailableError("courier_base_url is not configured")

        url = f"{settings.courier_base_url.rstrip('/')}{API_PREFIX}{path}"
        timeout = settings.courier_request_timeout_seconds
        last_exc: Optional[Exception] = None

        for attempt in range(2):
            try:
                async with httpx.AsyncClient(timeout=timeout) as client:
                    response = await client.request(
                        method, url, json=json, params=params, headers=CourierService._headers()
                    )
            except (httpx.TimeoutException, httpx.ConnectError) as exc:
                last_exc = exc
                if attempt == 0:
                    logger.warning(f"Courier {method} {path} timed out/unreachable, retrying once")
                    continue
                break

            if response.status_code >= 500 and attempt == 0:
                logger.warning(f"Courier {method} {path} returned {response.status_code}, retrying once")
                continue

            if response.status_code == 429 and attempt == 0:
                # Honour Retry-After, but only if it is short enough to be
                # worth waiting for inside a request cycle. Anything longer is
                # an outage as far as this checkout is concerned.
                delay = min(float(response.headers.get("Retry-After") or 1), timeout)
                logger.warning(f"Courier {method} {path} rate-limited, retrying in {delay}s")
                await asyncio.sleep(delay)
                continue

            if response.status_code >= 400:
                CourierService._raise_for_error(response, path)

            if response.status_code == 204 or not response.content:
                return {}
            return response.json()

        raise CourierUnavailableError(f"Courier {method} {path} unreachable: {last_exc}")

    # ------------------------------------------------------------------
    # Section 5 — locations
    # ------------------------------------------------------------------

    @staticmethod
    async def resolve_location(
        *,
        post_office: str,
        district: Optional[str] = None,
        province: Optional[str] = None,
    ) -> LocationResolution:
        """
        ``POST /ecommerce/locations/resolve`` (section 5a) — post office to
        ``city_id``. Always a ``200``; an unresolvable address comes back as
        ``resolved=False`` with a reason, not an exception.

        ``district`` is sent whenever we have it: it is the practical
        disambiguator for duplicate post-office names, and omitting it is what
        produces ``ambiguous_post_office``.
        """
        payload: Dict[str, Any] = {"post_office": post_office}
        if district:
            payload["district"] = district
        if province:
            payload["province"] = province

        data = await CourierService._request("POST", "/locations/resolve", json=payload)
        if data.get("resolved"):
            return LocationResolution(
                resolved=True,
                city_id=data.get("city_id"),
                city_name=data.get("city_name"),
            )
        return LocationResolution(
            resolved=False,
            reason=data.get("reason"),
            candidates=data.get("candidates") or [],
        )

    @staticmethod
    async def get_coverage() -> List[dict]:
        """
        ``GET /ecommerce/coverage`` (section 5c) — every post office that
        resolves serviceable right now. For the nightly sync that keeps our
        own branch-coverage table from accepting orders SribeesExpress cannot
        deliver.
        """
        data = await CourierService._request("GET", "/coverage")
        return data if isinstance(data, list) else []

    # ------------------------------------------------------------------
    # Section 6 — rates
    # ------------------------------------------------------------------

    @staticmethod
    async def get_rate(
        *,
        city_id: int,
        weight_kg: Decimal,
        payment_method: str,
        cod_amount: Decimal = Decimal("0"),
        latitude: Optional[float] = None,
        longitude: Optional[float] = None,
    ) -> CourierQuote:
        """
        ``POST /ecommerce/rates`` (section 6). Side-effect-free.

        ``cod_amount``/``payment_method`` are inert for pricing today but are
        sent anyway — the contract asks for them and commits to advance notice
        before they start affecting ``amount_lkr``, so sending them now means
        that change lands without a code change here.
        """
        payload: Dict[str, Any] = {
            "city_id": city_id,
            "weight_kg": float(weight_kg),
            "cod_amount": float(cod_amount),
            "payment_method": payment_method,
        }
        if latitude is not None and longitude is not None:
            payload["latitude"] = latitude
            payload["longitude"] = longitude

        data = await CourierService._request("POST", "/rates", json=payload)

        if not data.get("serviceable"):
            return CourierQuote(
                quote_id=None,
                amount_lkr=Decimal("0"),
                serviceable=False,
                reason=data.get("reason"),
            )
        return CourierQuote(
            quote_id=data.get("quote_id"),
            amount_lkr=_decimal(data.get("amount_lkr")) or Decimal("0"),
            serviceable=True,
            expires_at=_parse_datetime(data.get("expires_at")),
        )

    # ------------------------------------------------------------------
    # Section 7 / 7a / 8 / 8a / 9 — shipments
    # ------------------------------------------------------------------

    @staticmethod
    async def book_shipment(
        *,
        merchant_order_ref: str,
        quote_id: Optional[str],
        recipient_name: str,
        recipient_phone: str,
        address_line1: Optional[str] = None,
        address_line2: Optional[str] = None,
        post_office: Optional[str] = None,
        district: Optional[str] = None,
        province: Optional[str] = None,
        postal_code: Optional[str] = None,
        cod_amount: Decimal = Decimal("0"),
        handover_code: Optional[str] = None,
        pickup_location_id: Optional[int] = None,
        requested_delivery_date: Optional[str] = None,
        requested_delivery_window: Optional[str] = None,
        handling: Optional[List[str]] = None,
    ) -> dict:
        """
        ``POST /ecommerce/shipments`` (section 7). Idempotent on
        ``merchant_order_ref`` — safe to retry after a network failure; a
        replay returns the existing shipment with ``200`` instead of ``201``.

        We send both the flat ``recipient_address`` and the structured fields.
        The structured ones win server-side, and sending the flat string too
        costs nothing while keeping the payload readable in a support log.

        ``handover_code`` is passed straight through: the caller decides
        whether an order is eligible for one, since SribeesExpress accepts a
        code on prepaid shipments only (section 7).
        """
        payload: Dict[str, Any] = {
            "merchant_order_ref": merchant_order_ref,
            "quote_id": quote_id,
            "recipient_name": recipient_name,
            "recipient_phone": recipient_phone,
            "recipient_address": format_recipient_address(
                address_line1, address_line2, post_office, district, postal_code
            ),
            "cod_amount": float(cod_amount),
        }
        # Structured address (section 7) -- only the parts we actually have.
        for key, value in (
            ("address_line1", address_line1),
            ("address_line2", address_line2),
            ("post_office", post_office),
            ("district", district),
            ("province", province),
            ("postal_code", postal_code),
        ):
            if value:
                payload[key] = value

        if handover_code:
            # Both fields or neither -- one without the other is a documented
            # 400 (handover_code_required_without_code).
            payload["handover_code"] = handover_code
            payload["handover_code_required"] = True
        if pickup_location_id is not None:
            payload["pickup_location_id"] = pickup_location_id
        if requested_delivery_date:
            payload["requested_delivery_date"] = requested_delivery_date
        if requested_delivery_window:
            payload["requested_delivery_window"] = requested_delivery_window
        if handling:
            payload["handling"] = handling

        return await CourierService._request("POST", "/shipments", json=payload)

    @staticmethod
    async def get_shipment(shipment_id: str) -> dict:
        """``GET /ecommerce/shipments/{id}`` (section 8) — includes ``history``."""
        return await CourierService._request("GET", f"/shipments/{shipment_id}")

    @staticmethod
    async def list_shipments(
        *,
        updated_since: Optional[datetime] = None,
        status_filter: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> List[dict]:
        """
        ``GET /ecommerce/shipments`` (section 7a) — the reconciliation poll.

        This, not per-shipment reads, is the fallback for SribeesExpress
        having no webhook retry (section 10.4): 500 open parcels would be 500
        requests against a 60/min limit, which does not fit. Same per-shipment
        shape as section 7, minus ``history``.
        """
        params: Dict[str, Any] = {"limit": limit, "offset": offset}
        if updated_since is not None:
            params["updated_since"] = updated_since.isoformat()
        if status_filter:
            params["status_filter"] = status_filter

        data = await CourierService._request("GET", "/shipments", params=params)
        return data if isinstance(data, list) else []

    @staticmethod
    async def patch_shipment(shipment_id: str, **fields: Any) -> dict:
        """
        ``PATCH /ecommerce/shipments/{id}`` (section 8a) — correct a typo'd
        phone or address before the parcel goes out for delivery. Raises
        ``CourierError('shipment_not_correctable')`` once it is too late.
        """
        payload = {k: v for k, v in fields.items() if v is not None}
        return await CourierService._request("PATCH", f"/shipments/{shipment_id}", json=payload)

    @staticmethod
    async def cancel_shipment(shipment_id: str) -> dict:
        """
        ``POST /ecommerce/shipments/{id}/cancel`` (section 9). Legal only
        before pickup; past that it is a ``409`` and a support conversation.
        """
        return await CourierService._request("POST", f"/shipments/{shipment_id}/cancel")

    # ------------------------------------------------------------------
    # Section 12 — pickup locations
    # ------------------------------------------------------------------

    @staticmethod
    async def create_pickup_location(
        *,
        name: str,
        phone: Optional[str],
        address: str,
        post_office: Optional[str],
        district: Optional[str],
        province: Optional[str],
        latitude: Optional[float] = None,
        longitude: Optional[float] = None,
    ) -> dict:
        """
        ``POST /ecommerce/pickup-locations`` (section 12) — register one of
        our branches as a collection point. Returns the created location; its
        ``id`` is what we send as ``pickup_location_id`` at booking time.
        """
        payload: Dict[str, Any] = {"name": name, "address": address}
        for key, value in (
            ("phone", phone),
            ("post_office", post_office),
            ("district", district),
            ("province", province),
            ("latitude", latitude),
            ("longitude", longitude),
        ):
            if value is not None:
                payload[key] = value
        return await CourierService._request("POST", "/pickup-locations", json=payload)

    @staticmethod
    async def update_pickup_location(location_id: int, **fields: Any) -> dict:
        """
        ``PATCH /ecommerce/pickup-locations/{id}`` (section 12).

        Note this never affects shipments already booked — their pickup
        address is a snapshot taken at booking time, not a live reference.
        """
        payload = {k: v for k, v in fields.items() if v is not None}
        return await CourierService._request("PATCH", f"/pickup-locations/{location_id}", json=payload)

    @staticmethod
    async def list_pickup_locations() -> List[dict]:
        """``GET /ecommerce/pickup-locations`` (section 12)."""
        data = await CourierService._request("GET", "/pickup-locations")
        return data if isinstance(data, list) else []

    # ------------------------------------------------------------------
    # Section 10.1 — webhook endpoint registration
    # ------------------------------------------------------------------

    @staticmethod
    async def register_webhook_endpoint(*, url: str, description: str = "") -> dict:
        """
        ``POST /ecommerce/webhook-endpoints`` (section 10.1).

        The response's ``secret`` is shown **exactly once** — the caller must
        persist it into ``courier_webhook_secret`` immediately; it can never
        be read back, only rotated.
        """
        return await CourierService._request(
            "POST", "/webhook-endpoints", json={"url": url, "description": description}
        )
