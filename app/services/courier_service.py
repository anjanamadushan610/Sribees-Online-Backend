"""
Courier Service — HTTP client for SribeesExpress's ``/api/v1/ecommerce/*`` API.

Contract: ``docs/SRIBEESEXPRESS_ECOMMERCE_API.md``. SRIBEESonline is the
client; SribeesExpress exposes the API. Every call sends ``X-Api-Key``: the key
of the enclosing ``courier_credentials`` scope.

Four things about this contract shape the client:

* **The key decides the price's origin.** A delivery is priced from the calling
  account's registered postal city (or a named outlet's) to the destination's.
  A branch holding its own key is its own SribeesExpress client, so quoting
  inside that branch's scope prices from that branch — which is why every
  quote must run inside the fulfilling branch's credential scope.
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
  (``CourierUnavailableError``) from a real answer such as
  ``CourierQuoteExpiredError`` (re-quote) or ``serviceable: false`` (a normal
  UI state). There is no flat delivery fee behind either any more: a delivery
  we cannot price is a checkout we refuse, honestly, rather than a guess.
"""
import asyncio
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional, Sequence, Union

import httpx
from loguru import logger

from app.config.settings import settings

# The contract's own path prefix. The active credential scope holds the origin
# only, so that a deployment can point at a mock/staging host without also
# having to know the API's internal layout.
API_PREFIX = "/api/v1/ecommerce"

# Documented field maxima (contract section 15, the round-2 field answers).
# Enforced client-side: exceeding them is a 422 at booking time, and a booking
# that fails on a long name is an order that silently never ships.
RECIPIENT_ADDRESS_MAX = 255
RECIPIENT_NAME_MAX = 150
RECIPIENT_PHONE_MAX = 30
ADDRESS_LINE_MAX = 150
# `reason` is a VARCHAR(255) on their side and a long customer comment is
# the normal case, so trim here rather than let the whole booking 422.
RETURN_REASON_MAX = 255

# Section 7a caps `limit` at 200. Asking for more is rejected outright, so the
# client clamps rather than letting a misconfigured setting break every sweep.
SHIPMENT_LIST_LIMIT_MAX = 200

#: The contract's two payment methods (sections 6 and 7).
COD = "cod"
PREPAID = "prepaid"


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
    """Result of ``POST /ecommerce/rates`` (contract section 6) or its confirm."""

    quote_id: Optional[str]
    amount_lkr: Decimal
    serviceable: bool
    expires_at: Optional[datetime] = None
    reason: Optional[str] = None
    origin_postal_city_id: Optional[int] = None
    confirmed: bool = False


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


def _quote_from(data: dict) -> CourierQuote:
    if not data.get("serviceable"):
        return CourierQuote(
            quote_id=None,
            amount_lkr=Decimal("0"),
            serviceable=False,
            reason=data.get("reason"),
            origin_postal_city_id=data.get("origin_postal_city_id"),
        )
    return CourierQuote(
        quote_id=data.get("quote_id"),
        amount_lkr=_decimal(data.get("amount_lkr")) or Decimal("0"),
        serviceable=True,
        expires_at=_parse_datetime(data.get("expires_at")),
        origin_postal_city_id=data.get("origin_postal_city_id"),
        confirmed=bool(data.get("confirmed")),
    )


def format_recipient_address(
    address_line1: Optional[str],
    address_line2: Optional[str] = None,
    postal_city: Optional[str] = None,
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
    parts = [
        p.strip()
        for p in (address_line1, address_line2, postal_city, district, postal_code)
        if p and p.strip()
    ]
    joined = ", ".join(parts)
    return joined[:RECIPIENT_ADDRESS_MAX]


class CourierService:
    """Client for SribeesExpress's rate/shipment/tracking/outlet endpoints."""

    # ------------------------------------------------------------------
    # Transport
    # ------------------------------------------------------------------

    @staticmethod
    def _target():
        """
        Whose account this call acts as: host, key and environment.

        Comes from the enclosing ``courier_credentials`` scope. There is no
        fallback -- the host and key live in the database, not in deployment
        config, so "no scope" means a caller forgot to open one rather than
        "use the default", and guessing would send a merchant's parcels into
        whichever account happened to be configured last.

        Imported inside the function because courier_credentials imports this
        module's exceptions.
        """
        from app.services import courier_credentials

        target = courier_credentials.active_target()
        if target is None:
            raise CourierUnavailableError(
                "No SribeesExpress credentials are in scope for this call. Wrap it in "
                "courier_credentials.for_branch(...) or .for_account(...)."
            )
        return target

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

        The host comes from the active credential scope. A call made outside
        one raises ``CourierUnavailableError`` before touching the network:
        an empty base URL would otherwise raise ``httpx.InvalidURL``, which
        the except clause below does not catch.
        """
        target = CourierService._target()
        url = f"{target.base_url.rstrip('/')}{API_PREFIX}{path}"
        timeout = settings.courier_request_timeout_seconds
        last_exc: Optional[Exception] = None

        for attempt in range(2):
            try:
                async with httpx.AsyncClient(timeout=timeout) as client:
                    response = await client.request(
                        method,
                        url,
                        json=json,
                        params=params,
                        headers={
                            "X-Api-Key": target.api_key,
                            "Content-Type": "application/json",
                        },
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
    # Section 5 — postal cities
    # ------------------------------------------------------------------

    @staticmethod
    async def list_postal_cities(
        *,
        search: Optional[str] = None,
        district: Optional[str] = None,
        province: Optional[str] = None,
    ) -> List[dict]:
        """
        ``GET /ecommerce/postal-cities`` (section 5) — every postal city
        SribeesExpress delivers to today, ``{id, name, district, province}``.

        The whole list in one response; synced daily into our directory by
        ``courier_postal_city_service`` rather than called per checkout.
        """
        params = {k: v for k, v in (("search", search), ("district", district), ("province", province)) if v}
        data = await CourierService._request("GET", "/postal-cities", params=params or None)
        return data if isinstance(data, list) else []

    # ------------------------------------------------------------------
    # Section 6 — rates
    # ------------------------------------------------------------------

    @staticmethod
    async def get_rate(
        *,
        postal_city_id: int,
        weight_kg: Decimal,
        payment_method: str,
        cod_amount: Decimal = Decimal("0"),
        outlet_id: Optional[int] = None,
        latitude: Optional[float] = None,
        longitude: Optional[float] = None,
    ) -> CourierQuote:
        """
        ``POST /ecommerce/rates`` (section 6). Side-effect-free.

        Priced from the scope's account's registered postal city, or from
        ``outlet_id``'s when given — the one a branch booking under the shared
        account key must name, or it would be priced from the account's head
        office.

        ``cod_amount``/``payment_method`` are inert for pricing today but are
        sent anyway — the contract asks for them and commits to advance notice
        before they start affecting ``amount_lkr``.
        """
        payload: Dict[str, Any] = {
            "postal_city_id": postal_city_id,
            "weight_kg": float(weight_kg),
            "cod_amount": float(cod_amount),
            "payment_method": payment_method,
        }
        if outlet_id is not None:
            payload["outlet_id"] = outlet_id
        if latitude is not None and longitude is not None:
            payload["latitude"] = latitude
            payload["longitude"] = longitude

        return _quote_from(await CourierService._request("POST", "/rates", json=payload))

    @staticmethod
    async def confirm_quote(quote_id: str) -> CourierQuote:
        """
        ``POST /ecommerce/rates/{quote_id}/confirm`` — hold the quoted price for
        the fulfilment window, once the customer has committed to it.

        Idempotent. Raises ``CourierQuoteExpiredError`` for a quote past its
        thirty minutes; the caller re-quotes and asks the customer again rather
        than holding a price nobody agreed to.
        """
        return _quote_from(await CourierService._request("POST", f"/rates/{quote_id}/confirm"))

    # ------------------------------------------------------------------
    # Section 7 / 7a / 8 / 8a / 9 — shipments
    # ------------------------------------------------------------------

    @staticmethod
    async def book_shipment(
        *,
        merchant_order_ref: str,
        quote_id: Optional[str],
        payment_method: str,
        recipient_name: str,
        recipient_phone: str,
        address_line1: Optional[str] = None,
        address_line2: Optional[str] = None,
        postal_city: Optional[str] = None,
        district: Optional[str] = None,
        province: Optional[str] = None,
        postal_code: Optional[str] = None,
        cod_amount: Decimal = Decimal("0"),
        handover_code: Optional[str] = None,
        outlet_id: Optional[int] = None,
        requested_delivery_date: Optional[str] = None,
        requested_delivery_window: Optional[str] = None,
        handling: Optional[List[str]] = None,
    ) -> dict:
        """
        ``POST /ecommerce/shipments`` (section 7). Idempotent on
        ``merchant_order_ref`` — safe to retry after a network failure; a
        replay returns the existing shipment with ``200`` instead of ``201``.

        ``payment_method`` is sent explicitly and must agree with
        ``cod_amount``: SribeesExpress refuses a ``cod`` booking with nothing
        to collect and a ``prepaid`` one with an amount. ``postal_city`` is
        only used for the flat ``recipient_address`` rendering — the booked
        destination is the quote's, by construction.

        ``handover_code`` is passed straight through: the caller decides
        whether an order is eligible for one, and a code on a COD booking is a
        documented ``400``.
        """
        payload: Dict[str, Any] = {
            "merchant_order_ref": merchant_order_ref,
            "payment_method": payment_method,
            "recipient_name": (recipient_name or "")[:RECIPIENT_NAME_MAX],
            "recipient_phone": (recipient_phone or "")[:RECIPIENT_PHONE_MAX],
            "recipient_address": format_recipient_address(
                address_line1, address_line2, postal_city, district, postal_code
            ),
            "cod_amount": float(cod_amount),
        }
        # Structured address (section 7) -- only the parts we actually have.
        # The postal city itself is not one of them: SribeesExpress inserts the
        # quote's postal city name after address_line2 on its side.
        for key, value in (
            ("address_line1", address_line1),
            ("address_line2", address_line2),
            ("district", district),
            ("province", province),
            ("postal_code", postal_code),
        ):
            if value:
                payload[key] = value[:ADDRESS_LINE_MAX] if key.startswith("address_line") else value

        # Omitted rather than sent as null when we have no quote: SribeesExpress
        # types quote_id as a string and answers an explicit null with a 422.
        if quote_id:
            payload["quote_id"] = quote_id

        if handover_code:
            # Both fields or neither -- one without the other is a documented
            # 400 (handover_code_required_without_code).
            payload["handover_code"] = handover_code
            payload["handover_code_required"] = True
        if outlet_id is not None:
            payload["outlet_id"] = outlet_id
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
        status_filter: Optional[Union[str, Sequence[str]]] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> List[dict]:
        """
        ``GET /ecommerce/shipments`` (section 7a) -- the reconciliation poll.

        This, not per-shipment reads, is the fallback for SribeesExpress
        having no webhook retry (section 10.4). Same per-shipment shape as
        section 7 -- ``handover``, ``cod`` and ``remittance_id`` included --
        minus ``history``. ``updated_since`` is **inclusive** (``>=``).
        """
        params: Dict[str, Any] = {
            "limit": min(limit, SHIPMENT_LIST_LIMIT_MAX),
            "offset": offset,
        }
        if updated_since is not None:
            params["updated_since"] = updated_since.isoformat()
        if status_filter:
            if isinstance(status_filter, str):
                params["status_filter"] = status_filter
            else:
                params["status_filter"] = ",".join(status_filter)

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
    # Section 14 -- COD remittances (read-only)
    # ------------------------------------------------------------------

    # --- Returns (contract section 9a) -----------------------------------
    #
    # The reverse leg of a shipment we already booked: a rider collects from
    # the customer and brings the goods back to our branch. It gets its own
    # SXR waybill and its own fee, and our forward shipment is untouched.

    @staticmethod
    async def create_return(
        *,
        merchant_order_ref: str,
        reason: str,
        items: Optional[List[dict]] = None,
        pickup_contact_name: Optional[str] = None,
        pickup_contact_phone: Optional[str] = None,
        pickup_address: Optional[str] = None,
    ) -> dict:
        """
        ``POST /ecommerce/returns`` (section 9a.1).

        Idempotent on ``merchant_order_ref`` — the **original** shipment's ref,
        not a new one. A retry after a timeout returns the existing return
        instead of sending a second rider to a customer who has already handed
        the goods over, which is the failure this endpoint is shaped to avoid.

        The response carries ``handover_code`` **once**. Persist it on the
        order the moment this returns: SribeesExpress holds only a hash and
        cannot tell us what it was, and it is what our counter staff read to
        the rider to close the return.

        No quote step, unlike ``book_shipment``. A return is a consequence of a
        delivery that already happened on a corridor they already know, so they
        price it and report ``return_fee_lkr`` back.
        """
        payload: Dict[str, Any] = {
            "merchant_order_ref": merchant_order_ref,
            "reason": reason[:RETURN_REASON_MAX],
        }
        if items:
            payload["items"] = items
        for key, value in (
            ("pickup_contact_name", pickup_contact_name),
            ("pickup_contact_phone", pickup_contact_phone),
            ("pickup_address", pickup_address),
        ):
            if value:
                payload[key] = value
        return await CourierService._request("POST", "/returns", json=payload)

    @staticmethod
    async def get_return(return_id: str) -> dict:
        """``GET /ecommerce/returns/{id}`` (section 9a.2) — includes ``history``.
        ``handover_code`` is always null here; only the creating call has it."""
        return await CourierService._request("GET", f"/returns/{return_id}")

    @staticmethod
    async def regenerate_return_code(return_id: str) -> dict:
        """``POST /ecommerce/returns/{id}/regenerate-code`` (section 9a.3).

        The answer to "the branch lost the code", and the only one. Kills the
        old code immediately, so the value this returns must replace what is on
        the order rather than sit beside it.
        """
        return await CourierService._request("POST", f"/returns/{return_id}/regenerate-code")

    @staticmethod
    async def get_cod_balance() -> dict:
        """
        ``GET /ecommerce/cod-balance`` (section 14) -- cash collected from our
        customers that has not yet been paid out to us.
        """
        return await CourierService._request("GET", "/cod-balance")

    @staticmethod
    async def list_remittances(*, limit: int = 50, offset: int = 0) -> List[dict]:
        """``GET /ecommerce/remittances`` (section 14)."""
        data = await CourierService._request(
            "GET", "/remittances", params={"limit": limit, "offset": offset}
        )
        return data if isinstance(data, list) else []

    @staticmethod
    async def get_remittance_shipments(remittance_id: int) -> List[dict]:
        """``GET /ecommerce/remittances/{id}/shipments`` (section 14)."""
        data = await CourierService._request("GET", f"/remittances/{remittance_id}/shipments")
        return data if isinstance(data, list) else []

    # ------------------------------------------------------------------
    # Section 12 — outlets
    # ------------------------------------------------------------------

    @staticmethod
    async def create_outlet(
        *,
        name: str,
        phone: str,
        address: str,
        postal_city_id: int,
        latitude: Optional[float] = None,
        longitude: Optional[float] = None,
    ) -> dict:
        """
        ``POST /ecommerce/outlets`` (section 12) — register one of our branches
        as a collection point in the calling account. Its ``id`` is what a
        branch without its own key sends as ``outlet_id``.
        """
        payload: Dict[str, Any] = {
            "name": name,
            "phone": phone,
            "address": address,
            "postal_city_id": postal_city_id,
        }
        if latitude is not None and longitude is not None:
            payload["latitude"] = latitude
            payload["longitude"] = longitude
        return await CourierService._request("POST", "/outlets", json=payload)

    @staticmethod
    async def update_outlet(outlet_id: int, **fields: Any) -> dict:
        """
        ``PATCH /ecommerce/outlets/{id}`` (section 12). Never affects shipments
        already booked — their pickup address is a snapshot.
        """
        payload = {k: v for k, v in fields.items() if v is not None}
        return await CourierService._request("PATCH", f"/outlets/{outlet_id}", json=payload)

    @staticmethod
    async def list_outlets() -> List[dict]:
        """``GET /ecommerce/outlets`` (section 12)."""
        data = await CourierService._request("GET", "/outlets")
        return data if isinstance(data, list) else []

    # ------------------------------------------------------------------
    # Section 10.1 — webhook endpoint registration
    # ------------------------------------------------------------------

    @staticmethod
    async def register_webhook_endpoint(*, url: str, description: str = "") -> dict:
        """
        ``POST /ecommerce/webhook-endpoints`` (section 10.1).

        The response's ``secret`` is shown **exactly once** — the caller must
        store it (encrypted) immediately; it can never be read back, only
        rotated.
        """
        return await CourierService._request(
            "POST", "/webhook-endpoints", json={"url": url, "description": description}
        )

    @staticmethod
    async def list_webhook_endpoints() -> List[dict]:
        """``GET /ecommerce/webhook-endpoints`` (section 10.1) — never includes a secret."""
        data = await CourierService._request("GET", "/webhook-endpoints")
        return data if isinstance(data, list) else []

    @staticmethod
    async def rotate_webhook_secret(endpoint_id: int) -> dict:
        """``POST /ecommerce/webhook-endpoints/{id}/rotate-secret`` — the old secret dies at once."""
        return await CourierService._request("POST", f"/webhook-endpoints/{endpoint_id}/rotate-secret")
