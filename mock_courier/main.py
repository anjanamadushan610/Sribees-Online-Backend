"""
Mock Courier Server — DEV/TEST ONLY. NOT the real courier.

Stands in for SribeesExpress as described in
fastapi_backend/docs/SRIBEESEXPRESS_ECOMMERCE_API.md, so the SRIBEESonline
backend's client side (app/services/courier_service.py), the handover-code
flow and the signed-webhook path can be exercised end-to-end before real
sandbox keys are issued. Implements the contract's /api/v1/ecommerce/*
endpoints with deterministic pricing and forceable error scenarios — see
README.md in this directory.

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

MOCK_API_KEY = os.environ.get("MOCK_COURIER_API_KEY", "sk_test_sribees-mock-courier-key")

# Everything under this prefix is the contract; anything else (/, /docs) is
# mock plumbing and stays unauthenticated.
API_PREFIX = "/api/v1/ecommerce/"

app = FastAPI(
    title="Mock Courier (DEV/TEST ONLY)",
    description=(
        "Stand-in for fastapi_backend/docs/SRIBEESEXPRESS_ECOMMERCE_API.md. "
        "NOT the real courier -- never point production at this."
    ),
)


@app.middleware("http")
async def require_api_key(request: Request, call_next):
    """Real auth check (not a magic value) on every contract route, per section 3."""
    if request.url.path.startswith(API_PREFIX):
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
