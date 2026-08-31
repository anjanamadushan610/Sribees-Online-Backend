"""
POST /v1/rates — docs/COURIER_API.md section 4.

Deterministic pricing (documented in README.md so a test can assert an exact
figure) plus the forceable error scenarios a real courier's own sandbox is
asked to provide (section 11).
"""
import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Optional

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from mock_courier.state import STORE

router = APIRouter(tags=["rates"])

BASE = Decimal("300.00")
PER_KG_AFTER_FIRST = Decimal("60.00")
COD_FEE_RATE = Decimal("0.01")


def quote_amount(weight_kg: Decimal, order_value_lkr: Decimal, payment_method: str) -> Decimal:
    """base + max(0, weight_kg - 1) * per_kg + (1% of order value if COD)."""
    weight_surcharge = max(Decimal("0"), weight_kg - 1) * PER_KG_AFTER_FIRST
    cod_fee = (order_value_lkr * COD_FEE_RATE) if payment_method == "cod" else Decimal("0")
    return (BASE + weight_surcharge + cod_fee).quantize(Decimal("0.01"))


class RateRequest(BaseModel):
    origin: dict
    destination: dict
    parcel: dict
    order_value_lkr: float
    payment_method: str


def _error(status_code: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"error": {"code": code, "message": message}})


@router.post("/v1/rates")
async def get_rate(data: RateRequest):
    post_office = (data.destination or {}).get("post_office") or ""
    weight_kg = Decimal(str((data.parcel or {}).get("total_weight_kg", 0)))

    # --- Forceable scenarios (magic values documented in README.md) ---
    if weight_kg == Decimal("999"):
        return _error(422, "weight_exceeds_maximum", "Parcel weight exceeds the maximum this service accepts.")
    if post_office == "ERROR_500_TEST":
        return _error(500, "internal_error", "Simulated courier outage.")
    if post_office == "TIMEOUT_TEST":
        await asyncio.sleep(5)
    if post_office == "UNSERVICEABLE_TEST":
        return {"serviceable": False, "reason": "No delivery to this post office."}

    order_value = Decimal(str(data.order_value_lkr))
    amount = quote_amount(weight_kg, order_value, data.payment_method)
    quote_id = f"qt_{uuid.uuid4().hex[:12]}"
    # weight 998 -> a 5s-lived quote, so a test can observe real expiry (409
    # quote_expired at booking) without waiting the full 15 minutes.
    ttl_seconds = 5 if weight_kg == Decimal("998") else 15 * 60
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)

    STORE.quotes[quote_id] = {"amount_lkr": amount, "expires_at": expires_at}

    weight_surcharge = max(Decimal("0"), weight_kg - 1) * PER_KG_AFTER_FIRST
    cod_fee = (order_value * COD_FEE_RATE) if data.payment_method == "cod" else Decimal("0")

    return {
        "quote_id": quote_id,
        "amount_lkr": float(amount),
        "currency": "LKR",
        "service_level": "standard",
        "estimated_delivery_days": 2,
        "expires_at": expires_at.isoformat(),
        "breakdown": {
            "base": float(BASE),
            "weight_surcharge": float(weight_surcharge),
            "cod_fee": float(cod_fee),
        },
    }
