"""
Mock Courier Server — DEV/TEST ONLY. NOT the real courier.

Stands in for the courier described in fastapi_backend/docs/COURIER_API.md so
the SRIBEESonline backend's client side (app/services/courier_service.py) and
the delivery-verification-code flow (docs/COURIER_API.md section 15) can be
exercised end-to-end before the real, in-house courier service exists.
Implements the contract's four /v1/* endpoints with deterministic pricing and
forceable error scenarios — see README.md in this directory. This is exactly
what the contract's own section 11 ("Sandbox") asks any real courier build to
provide, so it can double as a reference for whoever builds that.

Never point a PRODUCTION courier_base_url at this. Pure stdlib + fastapi +
pydantic + httpx — no imports from app/, so this stays trivially deployable
and removable without touching the real backend.

Run standalone:
    cd fastapi_backend && uvicorn mock_courier.main:app --port 9100 --reload
"""
import os

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from mock_courier import rates, shipments

MOCK_API_KEY = os.environ.get("MOCK_COURIER_API_KEY", "sribees-mock-courier-key")

app = FastAPI(
    title="Mock Courier (DEV/TEST ONLY)",
    description=(
        "Stand-in for fastapi_backend/docs/COURIER_API.md. "
        "NOT the real courier -- never point production at this."
    ),
)


@app.middleware("http")
async def require_api_key(request: Request, call_next):
    """Real auth check (not a magic value) on every /v1/* route, per section 9."""
    if request.url.path.startswith("/v1/"):
        key = request.headers.get("x-api-key")
        if key != MOCK_API_KEY:
            return JSONResponse(
                status_code=401,
                content={"error": {"code": "unauthorized", "message": "Missing or invalid X-Api-Key"}},
            )
    return await call_next(request)


app.include_router(rates.router)
app.include_router(shipments.router)


@app.get("/")
async def root():
    return {"service": "mock-courier", "status": "DEV/TEST ONLY -- not the real courier"}
