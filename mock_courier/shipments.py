"""
Shipments, pickup locations and webhook endpoints — contract sections 7, 7a,
8, 8a, 9, 10.1 and 12.

Plus one mock-only test harness, ``POST .../_advance``, which drives a
shipment to any status -- including "the rider entered code X at the door" --
and pushes a properly **HMAC-signed** ``shipment.status_changed`` back into
the real backend. Signing it for real is the point: an unsigned convenience
path would let the backend's signature verification rot untested, and that
verification is the only thing standing between the orders table and the open
internet.
"""
import hashlib
import hmac
import json
import os
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import List, Optional

import httpx
from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from mock_courier.rates import quote_amount
from mock_courier.state import STORE

router = APIRouter(tags=["shipments"])

PREFIX = "/api/v1/ecommerce"
BACKEND_BASE_URL = os.environ.get("MOCK_COURIER_BACKEND_URL", "http://localhost:8000")
WEBHOOK_SECRET = os.environ.get("MOCK_COURIER_WEBHOOK_SECRET", "sribees-mock-webhook-secret")

# Statuses past which a shipment can no longer be corrected (section 8a) or
# cancelled (section 9).
LIST_LIMIT_MAX = 200

UNCORRECTABLE = {"out_for_delivery", "delivered", "failed", "returned", "cancelled"}
UNCANCELLABLE = {"picked_up", "in_transit", "out_for_delivery", "delivered", "failed", "returned"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _error(status_code: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"error": {"code": code, "message": message}})


class ShipmentRequest(BaseModel):
    merchant_order_ref: str
    quote_id: Optional[str] = None
    recipient_name: str
    recipient_phone: str
    recipient_address: Optional[str] = None
    address_line1: Optional[str] = None
    address_line2: Optional[str] = None
    post_office: Optional[str] = None
    district: Optional[str] = None
    province: Optional[str] = None
    postal_code: Optional[str] = None
    cod_amount: float = 0
    handover_code: Optional[str] = None
    handover_code_required: bool = False
    pickup_location_id: Optional[int] = None
    requested_delivery_date: Optional[str] = None
    requested_delivery_window: Optional[str] = None
    handling: List[str] = []


def _shipment_response(s: dict) -> dict:
    """Section 7's response shape, which section 7a and 8 also return."""
    return {
        "shipment_id": s["shipment_id"],
        "merchant_order_ref": s["merchant_order_ref"],
        "status": s["status"],
        "amount_lkr": s["amount_lkr"],
        "cod_amount": s["cod_amount"],
        "recipient_name": s["recipient_name"],
        "recipient_phone": s["recipient_phone"],
        "recipient_address": s["recipient_address"],
        "latitude": s.get("latitude"),
        "longitude": s.get("longitude"),
        "pickup_location_name": s.get("pickup_location_name"),
        "pickup_contact_phone": s.get("pickup_contact_phone"),
        "pickup_address": s.get("pickup_address"),
        "handover": s["handover"],
        "cod": s["cod"],
        "failure_note": s.get("failure_note"),
        "requested_delivery_date": s.get("requested_delivery_date"),
        "requested_delivery_window": s.get("requested_delivery_window"),
        "handling": s.get("handling") or [],
        "will_retry": s.get("will_retry"),
        "remittance_id": s.get("remittance_id"),
        "created_at": s["created_at"],
        "updated_at": s["updated_at"],
        "tracking_url": f"https://mock-courier.local/track?waybill={s['shipment_id']}",
    }


@router.post(f"{PREFIX}/shipments", status_code=201)
async def book_shipment(data: ShipmentRequest):
    # Idempotent on merchant_order_ref: a retried booking returns the existing
    # shipment with 200 rather than creating a second one (section 7).
    existing = STORE.shipments_by_ref.get(data.merchant_order_ref)
    if existing is not None:
        return JSONResponse(status_code=200, content=_shipment_response(existing))

    if data.handover_code_required and not data.handover_code:
        return _error(
            400,
            "handover_code_required_without_code",
            "handover_code_required: true needs a handover_code.",
        )
    if data.handover_code and data.cod_amount > 0:
        # Section 7: "Prepaid orders only -- never send this for a cod order."
        return _error(
            400,
            "handover_code_not_allowed_for_cod",
            "A handover code cannot be set on a COD shipment.",
        )
    if data.handover_code and not (4 <= len(data.handover_code) <= 6 and data.handover_code.isdigit()):
        return _error(400, "invalid_handover_code", "handover_code must be 4-6 digits.")

    pickup = None
    if data.pickup_location_id is not None:
        pickup = STORE.pickup_locations.get(data.pickup_location_id)
        if pickup is None:
            return _error(404, "pickup_location_not_found", "Unknown pickup_location_id.")
        if not pickup.get("is_active", True):
            return _error(
                409,
                "pickup_location_deactivated",
                "That pickup location was deactivated -- register a new one or omit it.",
            )

    latitude = longitude = None
    if data.quote_id is not None:
        quote = STORE.quotes.get(data.quote_id)
        if quote is None:
            return _error(404, "quote_not_found", "Unknown quote_id.")
        if quote["used"]:
            return _error(409, "quote_already_used", "This quote has already booked a shipment.")
        if quote["expires_at"] < datetime.now(timezone.utc):
            return _error(409, "quote_expired", "This quote has expired -- request a new one.")
        quote["used"] = True
        amount = float(quote["amount_lkr"])
        latitude, longitude = quote.get("latitude"), quote.get("longitude")
    else:
        # No prior quote -- the client's own /rates call failed and it fell
        # back to a flat fee. Price at today's live rate rather than rejecting
        # a booking for an order the customer has already paid for.
        amount = float(quote_amount(Decimal("1")))

    address = data.recipient_address or ", ".join(
        p for p in (data.address_line1, data.address_line2, data.post_office, data.district) if p
    )

    shipment_id = f"SXP{STORE.next_id():010d}"
    shipment = {
        "shipment_id": shipment_id,
        "merchant_order_ref": data.merchant_order_ref,
        "status": "booked",
        "amount_lkr": amount,
        "cod_amount": data.cod_amount,
        "recipient_name": data.recipient_name,
        "recipient_phone": data.recipient_phone,
        "recipient_address": address[:255],
        "latitude": latitude,
        "longitude": longitude,
        "pickup_location_name": (pickup or {}).get("name"),
        "pickup_contact_phone": (pickup or {}).get("phone"),
        "pickup_address": (pickup or {}).get("address"),
        "handover": {
            "required": data.handover_code_required,
            "code_verified": False if data.handover_code_required else None,
            "attempts": 0 if data.handover_code_required else None,
        },
        "handover_code": data.handover_code,
        "cod": {"collected": False, "amount": data.cod_amount},
        "failure_note": None,
        # Only meaningful once status is `failed` (section 8).
        "will_retry": None,
        # Set by _remit (below) once a remittance run covers this shipment.
        "remittance_id": None,
        "requested_delivery_date": data.requested_delivery_date,
        "requested_delivery_window": data.requested_delivery_window,
        "handling": data.handling,
        "created_at": _now(),
        "updated_at": _now(),
        "history": [{"status": "booked", "at": _now()}],
    }
    STORE.shipments_by_ref[data.merchant_order_ref] = shipment
    STORE.shipments_by_id[shipment_id] = shipment
    return _shipment_response(shipment)


@router.get(f"{PREFIX}/shipments")
async def list_shipments(
    updated_since: Optional[str] = None,
    status_filter: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
):
    """Section 7a -- the reconciliation poll. Same shape as section 7, no history."""
    rows = list(STORE.shipments_by_id.values())
    if updated_since:
        # Inclusive (>=), matching the contract: re-reading a boundary row is
        # free, skipping one is not.
        cutoff = datetime.fromisoformat(updated_since.replace("Z", "+00:00"))
        rows = [s for s in rows if datetime.fromisoformat(s["updated_at"]) >= cutoff]
    if status_filter:
        wanted = {v.strip() for v in status_filter.split(",") if v.strip()}
        rows = [s for s in rows if s["status"] in wanted]
    rows.sort(key=lambda s: s["updated_at"])
    return [_shipment_response(s) for s in rows[offset : offset + min(limit, LIST_LIMIT_MAX)]]


@router.get(f"{PREFIX}/shipments/{{shipment_id}}")
async def get_shipment(shipment_id: str):
    shipment = STORE.shipments_by_id.get(shipment_id)
    if shipment is None:
        return _error(404, "shipment_not_found", "Shipment not found.")
    payload = _shipment_response(shipment)
    payload["history"] = shipment["history"]
    return payload


class PatchShipmentRequest(BaseModel):
    recipient_phone: Optional[str] = None
    recipient_address: Optional[str] = None
    address_line1: Optional[str] = None
    address_line2: Optional[str] = None
    post_office: Optional[str] = None
    district: Optional[str] = None
    province: Optional[str] = None
    postal_code: Optional[str] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    requested_delivery_date: Optional[str] = None
    requested_delivery_window: Optional[str] = None


@router.patch(f"{PREFIX}/shipments/{{shipment_id}}")
async def patch_shipment(shipment_id: str, data: PatchShipmentRequest):
    """Section 8a -- correct a typo before the parcel is out for delivery."""
    shipment = STORE.shipments_by_id.get(shipment_id)
    if shipment is None:
        return _error(404, "shipment_not_found", "Shipment not found.")
    if shipment["status"] in UNCORRECTABLE:
        return _error(
            409,
            "shipment_not_correctable",
            f"A shipment in status {shipment['status']} can no longer be corrected.",
        )

    structured = ", ".join(
        p for p in (data.address_line1, data.address_line2, data.post_office, data.district) if p
    )
    if structured:
        shipment["recipient_address"] = structured[:255]
    elif data.recipient_address:
        shipment["recipient_address"] = data.recipient_address[:255]

    for key in ("recipient_phone", "latitude", "longitude", "requested_delivery_date", "requested_delivery_window"):
        value = getattr(data, key)
        if value is not None:
            shipment[key] = value

    shipment["updated_at"] = _now()
    return _shipment_response(shipment)


@router.post(f"{PREFIX}/shipments/{{shipment_id}}/cancel")
async def cancel_shipment(shipment_id: str):
    shipment = STORE.shipments_by_id.get(shipment_id)
    if shipment is None:
        return _error(404, "shipment_not_found", "Shipment not found.")
    if shipment["status"] in UNCANCELLABLE:
        return _error(409, "conflict", "The parcel has already been collected -- contact support.")
    shipment["status"] = "cancelled"
    shipment["updated_at"] = _now()
    shipment["history"].append({"status": "cancelled", "at": _now()})
    return _shipment_response(shipment)


# ---------------------------------------------------------------------------
# Section 12 -- pickup locations
# ---------------------------------------------------------------------------


class PickupLocationRequest(BaseModel):
    name: str
    phone: Optional[str] = None
    address: str
    post_office: Optional[str] = None
    district: Optional[str] = None
    province: Optional[str] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None


@router.post(f"{PREFIX}/pickup-locations", status_code=201)
async def create_pickup_location(data: PickupLocationRequest):
    # A name already registered is a 409, not a silent duplicate and not a
    # silent return-the-existing-row. A merchant re-running a registration
    # sync after losing its stored ids gets a clear error to recover from
    # rather than a forked branch registry.
    if any(
        loc["name"].lower() == data.name.lower() and loc.get("is_active", True)
        for loc in STORE.pickup_locations.values()
    ):
        return _error(
            409,
            "pickup_location_name_already_registered",
            f"A pickup location named {data.name!r} is already registered.",
        )
    location = data.model_dump()
    location["id"] = STORE.next_id()
    location["is_active"] = True
    STORE.pickup_locations[location["id"]] = location
    return location


@router.get(f"{PREFIX}/pickup-locations")
async def list_pickup_locations():
    return list(STORE.pickup_locations.values())


@router.patch(f"{PREFIX}/pickup-locations/{{location_id}}")
async def update_pickup_location(location_id: int, data: PickupLocationRequest):
    location = STORE.pickup_locations.get(location_id)
    if location is None:
        return _error(404, "pickup_location_not_found", "Unknown pickup location.")
    location.update({k: v for k, v in data.model_dump().items() if v is not None})
    return location


@router.delete(f"{PREFIX}/pickup-locations/{{location_id}}", status_code=204)
async def delete_pickup_location(location_id: int):
    location = STORE.pickup_locations.get(location_id)
    if location is not None:
        location["is_active"] = False
    return None


# ---------------------------------------------------------------------------
# Section 10.1 -- webhook endpoint registration
# ---------------------------------------------------------------------------


class WebhookEndpointRequest(BaseModel):
    url: str
    description: str = ""


@router.post(f"{PREFIX}/webhook-endpoints", status_code=201)
async def register_webhook_endpoint(data: WebhookEndpointRequest):
    if not data.url.startswith("https://"):
        return _error(422, "invalid_webhook_url", "Webhook URLs must be https://.")
    endpoint = {
        "id": STORE.next_id(),
        "url": data.url,
        "description": data.description,
        "is_active": True,
        # Shown exactly once, per the contract. The mock returns the shared
        # dev secret rather than a random one so a developer does not have to
        # re-copy it into .env after every restart.
        "secret": WEBHOOK_SECRET,
    }
    STORE.webhook_endpoints[endpoint["id"]] = endpoint
    return endpoint


# ---------------------------------------------------------------------------
# Mock-only test harness -- NOT part of the contract
# ---------------------------------------------------------------------------


def _sign(body: bytes) -> str:
    """Build the X-Sribees-Signature header exactly as section 10.3 defines it."""
    t = int(time.time())
    digest = hmac.new(WEBHOOK_SECRET.encode(), f"{t}.".encode() + body, hashlib.sha256).hexdigest()
    return f"t={t},v1={digest}"


async def _push(payload: dict) -> str:
    body = json.dumps(payload).encode()
    async with httpx.AsyncClient(timeout=5) as client:
        try:
            response = await client.post(
                f"{BACKEND_BASE_URL}/api/v1/courier/webhooks/shipment-status",
                content=body,
                headers={
                    "Content-Type": "application/json",
                    "X-Sribees-Event": "shipment.status_changed",
                    "X-Sribees-Signature": _sign(body),
                },
            )
            return f"{response.status_code}: {response.text[:200]}"
        except httpx.HTTPError as exc:
            return f"failed: {exc}"


class AdvanceRequest(BaseModel):
    status: str
    handover_code: Optional[str] = None
    cod_collected: Optional[bool] = None
    failure_note: Optional[str] = None
    will_retry: Optional[bool] = None


@router.post(f"{PREFIX}/shipments/{{shipment_id}}/_advance")
async def advance_shipment(shipment_id: str, data: AdvanceRequest):
    """
    TEST-HARNESS CONVENIENCE ONLY -- not part of the real contract (hence the
    underscore). Drives a shipment to any status and pushes a signed webhook,
    so the whole delivered/handover/COD path can be exercised locally without
    a rider app.

    A code-required shipment refuses to reach ``delivered`` without a matching
    ``handover_code``, mirroring the real service's rule -- otherwise local
    testing would pass on a path production rejects.
    """
    shipment = STORE.shipments_by_id.get(shipment_id)
    if shipment is None:
        return _error(404, "shipment_not_found", "Shipment not found.")

    if data.status == "delivered" and shipment["handover"]["required"]:
        shipment["handover"]["attempts"] = (shipment["handover"]["attempts"] or 0) + 1
        if data.handover_code != shipment.get("handover_code"):
            return _error(
                409,
                "handover_code_mismatch",
                "The rider's code does not match -- the parcel cannot be released.",
            )
        shipment["handover"]["code_verified"] = True

    shipment["status"] = data.status
    shipment["updated_at"] = _now()
    shipment["history"].append({"status": data.status, "at": _now()})

    if data.cod_collected:
        shipment["cod"] = {"collected": True, "amount": shipment["cod_amount"]}
    if data.failure_note is not None:
        shipment["failure_note"] = data.failure_note
    # will_retry only means anything on a `failed` shipment (section 8).
    shipment["will_retry"] = data.will_retry if data.status == "failed" else None

    payload = {
        "event": "shipment.status_changed",
        "event_id": str(uuid.uuid4()),
        "shipment_id": shipment_id,
        "status": data.status,
        "at": _now(),
    }
    if data.status == "delivered":
        payload["handover"] = shipment["handover"]
        payload["cod"] = shipment["cod"]
    if data.status in ("failed", "returned"):
        payload["failure_note"] = shipment["failure_note"]
        payload["will_retry"] = shipment["will_retry"]

    return {"shipment": _shipment_response(shipment), "webhook_push": await _push(payload)}

# ---------------------------------------------------------------------------
# Section 14 -- COD remittances (read-only on the real API)
# ---------------------------------------------------------------------------


def _unremitted() -> list:
    """Shipments whose COD was collected but not yet tied to a remittance."""
    return [
        s
        for s in STORE.shipments_by_id.values()
        if s["cod"].get("collected") and s.get("remittance_id") is None
    ]


@router.get(f"{PREFIX}/cod-balance")
async def cod_balance():
    rows = _unremitted()
    collected = sum(float(s["cod"].get("amount") or 0) for s in rows)
    return {
        "currency": "LKR",
        "collected_not_remitted": round(collected, 2),
        "shipment_count": len(rows),
        "oldest_collection_at": min((s["updated_at"] for s in rows), default=None),
    }


@router.get(f"{PREFIX}/remittances")
async def list_remittances(limit: int = 50, offset: int = 0):
    rows = sorted(STORE.remittances.values(), key=lambda r: r["created_at"], reverse=True)
    return rows[offset : offset + limit]


@router.get(f"{PREFIX}/remittances/{{remittance_id}}/shipments")
async def remittance_shipments(remittance_id: int):
    if remittance_id not in STORE.remittances:
        return _error(404, "remittance_not_found", "Unknown remittance.")
    return [
        _shipment_response(s)
        for s in STORE.shipments_by_id.values()
        if s.get("remittance_id") == remittance_id
    ]


class RemitRequest(BaseModel):
    status: str = "paid"
    bank_reference: Optional[str] = "CEFT-MOCK-0001"


@router.post(f"{PREFIX}/_remit")
async def remit(data: RemitRequest):
    """
    TEST-HARNESS CONVENIENCE ONLY -- not part of the real contract (hence the
    underscore). On the real service, generating a run and marking it paid are
    staff actions we can never call; this stands in for an ops person doing
    that, so the merchant side of the settlement flow can be tested.
    """
    rows = _unremitted()
    if not rows:
        return _error(409, "nothing_to_remit", "No collected-but-unremitted COD.")

    remittance_id = STORE.next_id()
    gross = sum(float(s["cod"].get("amount") or 0) for s in rows)
    deductions = sum(float(s["amount_lkr"] or 0) for s in rows)
    run = {
        "id": remittance_id,
        "gross_cod_amount": round(gross, 2),
        "deductions_amount": round(deductions, 2),
        "net_amount": round(gross - deductions, 2),
        "shipment_count": len(rows),
        "status": data.status,
        "bank_reference": data.bank_reference if data.status == "paid" else None,
        "created_at": _now(),
        "paid_at": _now() if data.status == "paid" else None,
    }
    STORE.remittances[remittance_id] = run
    for s in rows:
        s["remittance_id"] = remittance_id
        s["updated_at"] = _now()
    return run
