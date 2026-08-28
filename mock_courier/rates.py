"""
Locations and rates — contract sections 5, 5a, 5c and 6.

    POST /api/v1/ecommerce/locations/resolve
    GET  /api/v1/ecommerce/cities
    GET  /api/v1/ecommerce/coverage
    POST /api/v1/ecommerce/rates

Deterministic pricing (documented in README.md so a test can assert an exact
figure) plus the forceable scenarios a real sandbox is asked to provide
(contract section 11).
"""
import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Optional

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from mock_courier.state import CITY_DIRECTORY, STORE, UNCONFIGURED_POST_OFFICES

router = APIRouter(tags=["locations & rates"])

FIRST_KG = Decimal("100.00")
AFTER_KG_RATE = Decimal("50.00")

# Quote lifetime. The real contract says 30 minutes (section 6).
QUOTE_TTL_SECONDS = 30 * 60


def quote_amount(weight_kg: Decimal) -> Decimal:
    """
    first_kg + max(0, weight_kg - 1) * after_kg_rate.

    Weight-only, matching the real contract: ``cod_amount`` and
    ``payment_method`` are accepted but inert (section 6), and modelling a COD
    fee here would let a bug hide behind a mock that prices differently from
    production.
    """
    return (FIRST_KG + max(Decimal("0"), weight_kg - 1) * AFTER_KG_RATE).quantize(Decimal("0.01"))


def _error(status_code: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"error": {"code": code, "message": message}})


# ---------------------------------------------------------------------------
# Section 5a -- POST /locations/resolve
# ---------------------------------------------------------------------------


class ResolveRequest(BaseModel):
    post_office: str
    district: Optional[str] = None
    province: Optional[str] = None


@router.post("/api/v1/ecommerce/locations/resolve")
async def resolve_location(data: ResolveRequest):
    """Always 200 -- an unresolvable location is an answer, not a failure."""
    if data.post_office in UNCONFIGURED_POST_OFFICES:
        return {"resolved": False, "reason": "not_yet_configured", "candidates": []}
    if data.post_office == "NO_DELIVERY_TEST":
        return {"resolved": False, "reason": "no_delivery_to_this_area", "candidates": []}

    matches = [c for c in CITY_DIRECTORY if c["post_office"].lower() == data.post_office.lower()]
    if data.district:
        narrowed = [c for c in matches if c["district"].lower() == data.district.lower()]
        # Only narrow when it finds something -- a wrong district on an
        # otherwise unambiguous post office should still resolve, exactly as
        # the real service's "optional narrowing filter" wording implies.
        if narrowed:
            matches = narrowed

    if not matches:
        return {"resolved": False, "reason": "post_office_not_found", "candidates": []}
    if len(matches) > 1:
        return {
            "resolved": False,
            "reason": "ambiguous_post_office",
            "candidates": [
                {"district": c["district"], "province": c["province"]} for c in matches
            ],
        }

    match = matches[0]
    return {"resolved": True, "city_id": match["city_id"], "city_name": match["city_name"]}


# ---------------------------------------------------------------------------
# Sections 5b / 5c -- city lookup and coverage export
# ---------------------------------------------------------------------------


@router.get("/api/v1/ecommerce/cities")
async def list_cities(search: Optional[str] = None):
    rows = CITY_DIRECTORY
    if search:
        rows = [c for c in rows if search.lower() in c["city_name"].lower()]
    return [{"id": c["city_id"], "name": c["city_name"], "district": c["district"]} for c in rows]


@router.get("/api/v1/ecommerce/coverage")
async def get_coverage():
    """Every post office that would resolve serviceable right now."""
    return [
        {
            "post_office": c["post_office"],
            "district": c["district"],
            "province": c["province"],
            "city_id": c["city_id"],
        }
        for c in CITY_DIRECTORY
    ]


# ---------------------------------------------------------------------------
# Section 6 -- POST /rates
# ---------------------------------------------------------------------------


class RateRequest(BaseModel):
    city_id: int
    weight_kg: float
    origin_branch_id: Optional[int] = None
    cod_amount: float = 0
    payment_method: str = "prepaid"
    latitude: Optional[float] = None
    longitude: Optional[float] = None


@router.post("/api/v1/ecommerce/rates")
async def get_rate(data: RateRequest):
    weight_kg = Decimal(str(data.weight_kg))

    # --- Forceable scenarios (magic values documented in README.md) ---
    if weight_kg <= 0 or weight_kg > 1000:
        return _error(422, "invalid_weight", "weight_kg must be greater than 0 and at most 1000.")
    # The contract's own documented sandbox trigger (section 11).
    if weight_kg >= 900:
        return {
            "serviceable": False,
            "reason": "sandbox: simulated unserviceable area",
            "quote_id": None,
            "amount_lkr": None,
            "currency": "LKR",
            "expires_at": None,
            "breakdown": None,
        }
    if data.city_id == 500:
        return _error(500, "internal_error", "Simulated courier outage.")
    if data.city_id == 504:
        await asyncio.sleep(5)
    if not any(c["city_id"] == data.city_id for c in CITY_DIRECTORY):
        return {
            "serviceable": False,
            "reason": "No delivery to this city.",
            "quote_id": None,
            "amount_lkr": None,
            "currency": "LKR",
            "expires_at": None,
            "breakdown": None,
        }

    amount = quote_amount(weight_kg)
    quote_id = f"qt_{uuid.uuid4().hex[:22]}"
    # weight 899 -> a 5s-lived quote, so a test can observe real expiry (409
    # quote_expired at booking) without waiting the full 30 minutes.
    ttl = 5 if Decimal("898") < weight_kg < Decimal("900") else QUOTE_TTL_SECONDS
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=ttl)

    STORE.quotes[quote_id] = {
        "amount_lkr": amount,
        "expires_at": expires_at,
        "city_id": data.city_id,
        "latitude": data.latitude,
        "longitude": data.longitude,
        "used": False,
    }

    return {
        "serviceable": True,
        "reason": None,
        "quote_id": quote_id,
        "amount_lkr": float(amount),
        "currency": "LKR",
        "expires_at": expires_at.isoformat(),
        "breakdown": {
            "first_kg": str(FIRST_KG),
            "after_kg_rate": str(AFTER_KG_RATE),
            "weight_kg": str(weight_kg),
        },
    }
