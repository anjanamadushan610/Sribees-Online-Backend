"""
An in-process stand-in for the Marx IPG card gateway (API v4).

Like the fake courier, only the network is replaced: MarxIPGService still
builds its requests, sends the merchant key and parses the responses through
its own code. The test decides how the next payment turns out.
"""
from __future__ import annotations

import types
import uuid
from dataclasses import dataclass, field

import httpx
from fastapi import FastAPI, Header, HTTPException

MERCHANT_KEY = "marx_test_merchant_key"


@dataclass
class MarxState:
    outcome: str = "SUCCESS"  # SUCCESS | FAILURE | DOWN
    orders: dict = field(default_factory=dict)
    captures: list = field(default_factory=list)


def build_app(state: MarxState) -> FastAPI:
    app = FastAPI()

    def _auth(key):
        if key != MERCHANT_KEY:
            raise HTTPException(status_code=401, detail="bad merchant key")

    @app.post("/api/v4/ipg/orders")
    async def create(body: dict, merchant_api_key: str | None = Header(None, alias="merchant-api-key")):
        _auth(merchant_api_key)
        if state.outcome == "DOWN":
            raise HTTPException(status_code=503, detail="maintenance")
        tr_id = f"TR{uuid.uuid4().hex[:10].upper()}"
        state.orders[tr_id] = body
        return {"status": 0, "message": "OK",
                "data": {"trId": tr_id, "payUrl": f"https://pay.marx.test/{tr_id}"}}

    @app.put("/api/v4/ipg/orders/{tr_id}")
    async def capture(tr_id: str, body: dict,
                      merchant_api_key: str | None = Header(None, alias="merchant-api-key")):
        _auth(merchant_api_key)
        if tr_id not in state.orders:
            return {"status": 1, "message": "UNKNOWN_ORDER", "data": None}
        state.captures.append(tr_id)
        ok = state.outcome == "SUCCESS"
        return {"status": 0, "message": "OK", "data": {
            "summaryResult": "SUCCESS" if ok else "FAILURE",
            "gatewayResponse": {"response": {"gatewayCode": "APPROVED" if ok else "DECLINED"},
                                "order": {"status": "CAPTURED" if ok else "FAILED"}},
        }}

    @app.get("/api/v4/ipg/orders/{tr_id}/summary")
    async def summary(tr_id: str, merchant_api_key: str | None = Header(None, alias="merchant-api-key")):
        _auth(merchant_api_key)
        paid = tr_id in state.captures and state.outcome == "SUCCESS"
        return {"status": 0, "message": "OK", "data": {"summaryResult": "SUCCESS" if paid else "PENDING"}}

    return app


class _HttpxProxy(types.ModuleType):
    def __init__(self, app):
        super().__init__("httpx_fake_marx")
        self._app = app

    def __getattr__(self, name):
        return getattr(httpx, name)

    def AsyncClient(self, *args, **kwargs):  # noqa: N802 - mirrors httpx
        kwargs["transport"] = httpx.ASGITransport(app=self._app)
        return httpx.AsyncClient(*args, **kwargs)


def install(monkeypatch) -> MarxState:
    from app.config.settings import settings
    from app.services import marx_ipg_service

    state = MarxState()
    monkeypatch.setattr(marx_ipg_service, "httpx", _HttpxProxy(build_app(state)))
    monkeypatch.setattr(settings, "marx_ipg_api_key", MERCHANT_KEY)
    monkeypatch.setattr(settings, "marx_ipg_base_url", "https://marx.test")
    monkeypatch.setattr(settings, "marx_gateway_methods_enabled", True)
    return state
