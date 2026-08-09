"""
POST /v1/shipments, GET /v1/shipments/{id}, POST /v1/shipments/{id}/cancel —
docs/COURIER_API.md sections 5 and 10. Also the mock-only test-harness
POST /v1/shipments/{id}/simulate-delivery for docs/COURIER_API.md section 15.
"""
import os
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

import httpx
from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from mock_courier.rates import quote_amount
from mock_courier.state import STORE

router = APIRouter(tags=["shipments"])

BACKEND_BASE_URL = os.environ.get("MOCK_COURIER_BACKEND_URL", "http://localhost:8000")
BACKEND_INBOUND_KEY = os.environ.get("MOCK_COURIER_INBOUND_KEY", "sribees-mock-courier-inbound-key")


class ShipmentRequest(BaseModel):
    quote_id: Optional[str] = None
    order_number: str
    origin: dict
    destination: dict
    parcel: dict
    payment_method: str
    cod_amount_lkr: float = 0


def _shipment_response(shipment: dict) -> dict:
    return {
        "shipment_id": shipment["shipment_id"],
        "waybill_number": shipment["waybill_number"],
        "amount_lkr": shipment["amount_lkr"],
        "status": shipment["status"],
        "tracking_url": f"https://mock-courier.local/track/{shipment['waybill_number']}",
        "estimated_pickup_at": datetime.now(timezone.utc).isoformat(),
    }


@router.post("/v1/shipments", status_code=201)
async def book_shipment(data: ShipmentRequest):
    # Idempotent on order_number: a retried booking returns the existing
    # shipment with 200 rather than creating a second one (section 5).
    existing = STORE.shipments_by_order.get(data.order_number)
    if existing is not None:
        return JSONResponse(status_code=200, content=_shipment_response(existing))

    if data.quote_id is not None:
        quote = STORE.quotes.get(data.quote_id)
        if quote is None or quote["expires_at"] < datetime.now(timezone.utc):
            return JSONResponse(
                status_code=409,
                content={"error": {"code": "quote_expired", "message": "This quote is no longer valid."}},
            )
        amount = float(quote["amount_lkr"])
    else:
        # No prior quote (e.g. the client's own /v1/rates call failed and it
        # fell back to a flat fee) -- price at today's live rate instead of
        # rejecting the booking (see docs/COURIER_API.md Open Risks note).
        weight_kg = Decimal(str((data.parcel or {}).get("total_weight_kg", 0)))
        amount = float(
            quote_amount(weight_kg, Decimal(str(data.cod_amount_lkr or 0)), data.payment_method)
        )

    shipment_id = f"shp_{uuid.uuid4().hex[:12]}"
    waybill_number = f"MOCK{uuid.uuid4().hex[:10].upper()}"
    shipment = {
        "shipment_id": shipment_id,
        "waybill_number": waybill_number,
        "order_number": data.order_number,
        "amount_lkr": amount,
        "status": "booked",
        "history": [{"status": "booked", "at": datetime.now(timezone.utc).isoformat()}],
    }
    STORE.shipments_by_order[data.order_number] = shipment
    STORE.shipments_by_id[shipment_id] = shipment
    return _shipment_response(shipment)


@router.get("/v1/shipments/{shipment_id}")
async def get_shipment(shipment_id: str):
    shipment = STORE.shipments_by_id.get(shipment_id)
    if shipment is None:
        raise HTTPException(status_code=404, detail="Shipment not found")
    return {
        "shipment_id": shipment["shipment_id"],
        "waybill_number": shipment["waybill_number"],
        "status": shipment["status"],
        "updated_at": shipment["history"][-1]["at"],
        "history": shipment["history"],
    }


@router.post("/v1/shipments/{shipment_id}/cancel")
async def cancel_shipment(shipment_id: str):
    shipment = STORE.shipments_by_id.get(shipment_id)
    if shipment is None:
        raise HTTPException(status_code=404, detail="Shipment not found")
    shipment["status"] = "cancelled"
    shipment["history"].append({"status": "cancelled", "at": datetime.now(timezone.utc).isoformat()})
    return {"shipment_id": shipment_id, "status": "cancelled"}


class SimulateDeliveryRequest(BaseModel):
    code: str


@router.post("/v1/shipments/{shipment_id}/simulate-delivery")
async def simulate_delivery(shipment_id: str, data: SimulateDeliveryRequest):
    """
    TEST-HARNESS CONVENIENCE ONLY — not part of the real courier contract.
    Lets a developer manually drive "the rider entered code X at the door"
    during local testing without building a rider app: marks the shipment
    delivered and calls the real backend's inbound webhooks with it.
    """
    shipment = STORE.shipments_by_id.get(shipment_id)
    if shipment is None:
        raise HTTPException(status_code=404, detail="Shipment not found")

    shipment["status"] = "delivered"
    shipment["history"].append({"status": "delivered", "at": datetime.now(timezone.utc).isoformat()})

    order_number = shipment["order_number"]
    result: dict = {"shipment_status_pushed": False, "code_confirm": None}

    async with httpx.AsyncClient(timeout=5) as client:
        try:
            await client.post(
                f"{BACKEND_BASE_URL}/api/v1/courier/webhooks/shipment-status",
                json={
                    "shipment_id": shipment_id,
                    "status": "delivered",
                    "at": datetime.now(timezone.utc).isoformat(),
                },
                headers={"X-Api-Key": BACKEND_INBOUND_KEY},
            )
            result["shipment_status_pushed"] = True
        except httpx.HTTPError as exc:
            result["shipment_status_pushed"] = f"failed: {exc}"

        try:
            resp = await client.post(
                f"{BACKEND_BASE_URL}/api/v1/courier/webhooks/orders/{order_number}/delivery-code/confirm",
                json={"waybill_number": shipment["waybill_number"], "code": data.code},
                headers={"X-Api-Key": BACKEND_INBOUND_KEY},
            )
            result["code_confirm"] = resp.json()
        except httpx.HTTPError as exc:
            result["code_confirm"] = f"failed: {exc}"

    return result
