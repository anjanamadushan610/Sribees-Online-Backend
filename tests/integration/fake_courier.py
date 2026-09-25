"""
An in-process stand-in for the SribeesExpress e-commerce API.

Only the courier is faked, and only at the network: CourierService still
builds the URL, sends the X-Api-Key header and parses the response through its
real code. The fake answers the endpoints checkout and dispatch touch, per the
contract in docs/SRIBEESEXPRESS_ECOMMERCE_API.md, and records every call so a
test can assert what we asked the courier to do.
"""
from __future__ import annotations

import types
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import httpx
from fastapi import FastAPI, Header, HTTPException, Request

FEE_LKR = Decimal("350.00")
API_KEY = "sk_test_integration_fake"


@dataclass
class CourierState:
    fee: Decimal = FEE_LKR
    serviceable: bool = True
    calls: list = field(default_factory=list)
    quotes: dict = field(default_factory=dict)
    shipments: dict = field(default_factory=dict)

    def paths(self) -> list[str]:
        return [f"{m} {p}" for m, p, _ in self.calls]


def build_app(state: CourierState) -> FastAPI:
    app = FastAPI()
    prefix = "/api/v1/ecommerce"

    def _auth(key: str | None):
        if key != API_KEY:
            raise HTTPException(status_code=401, detail={"error": {"code": "unauthorized"}})

    @app.middleware("http")
    async def _record(request: Request, call_next):
        body = await request.body()
        state.calls.append((request.method, request.url.path.removeprefix(prefix), body))
        return await call_next(request)

    @app.post(f"{prefix}/rates")
    async def rates(payload: dict, x_api_key: str | None = Header(None)):
        _auth(x_api_key)
        if not state.serviceable:
            return {"serviceable": False, "reason": "outside coverage"}
        quote_id = f"q_{uuid.uuid4().hex[:12]}"
        state.quotes[quote_id] = {"payload": payload, "confirmed": False}
        return {
            "serviceable": True,
            "quote_id": quote_id,
            "amount_lkr": str(state.fee),
            "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat(),
        }

    @app.post(prefix + "/rates/{quote_id}/confirm")
    async def confirm(quote_id: str, x_api_key: str | None = Header(None)):
        _auth(x_api_key)
        if quote_id not in state.quotes:
            raise HTTPException(status_code=404, detail={"error": {"code": "quote_not_found"}})
        state.quotes[quote_id]["confirmed"] = True
        return {
            "serviceable": True,
            "quote_id": quote_id,
            "amount_lkr": str(state.fee),
            "confirmed": True,
            "expires_at": (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat(),
        }

    @app.api_route(prefix + "/{rest:path}", methods=["GET", "POST", "PATCH", "DELETE"])
    async def anything_else(rest: str, x_api_key: str | None = Header(None)):
        _auth(x_api_key)
        return {}

    return app


class _HttpxProxy(types.ModuleType):
    """httpx, except AsyncClient talks to the fake courier."""

    def __init__(self, app: FastAPI):
        super().__init__("httpx_fake_courier")
        self._app = app

    def __getattr__(self, name):
        return getattr(httpx, name)

    def AsyncClient(self, *args, **kwargs):  # noqa: N802 - mirrors httpx
        kwargs["transport"] = httpx.ASGITransport(app=self._app)
        return httpx.AsyncClient(*args, **kwargs)


def install(monkeypatch) -> CourierState:
    from app.services import courier_credentials, courier_service

    state = CourierState()
    monkeypatch.setattr(courier_service, "httpx", _HttpxProxy(build_app(state)))

    async def _resolve(db, branch_id=None):
        return courier_credentials.CourierTarget(
            base_url="http://courier.test",
            api_key=API_KEY,
            environment="test",
            key_source="branch",
            branch_id=branch_id,
        )

    monkeypatch.setattr(courier_credentials, "resolve", _resolve)
    return state
