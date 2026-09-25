"""
GET /api/v1/branch/public/{id}: the name a shared product link shows.

Unit-level: the DB dependency and the lookup are stubbed.
"""
from types import SimpleNamespace
from uuid import UUID

import httpx
import pytest

from app.config.database import get_db
from app.services import branch_service

BID = "5b0c1a52-7f3e-4b8e-9a0e-2d1f3c4b5a69"


async def _no_db():
    yield None


@pytest.fixture
def client():
    from app.main import app

    app.dependency_overrides[get_db] = _no_db
    yield httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://api.sribees.com")
    app.dependency_overrides.pop(get_db, None)


def _stub(monkeypatch, branch):
    async def lookup(db, branch_id):
        return branch

    monkeypatch.setattr(branch_service, "get_public_branch", lookup)


@pytest.mark.asyncio
async def test_a_live_branch_shows_only_its_public_name(client, monkeypatch):
    _stub(
        monkeypatch,
        SimpleNamespace(
            branch_id=UUID(BID), name="Kandy", district="Kandy",
            phone="0812345678", courier_api_key_live="secret",
        ),
    )
    async with client as c:
        r = await c.get(f"/api/v1/branch/public/{BID}")

    assert r.status_code == 200
    assert r.json()["data"] == {"branchId": BID, "branchName": "Kandy", "district": "Kandy"}


@pytest.mark.asyncio
async def test_an_unknown_or_inactive_branch_is_not_found(client, monkeypatch):
    _stub(monkeypatch, None)
    async with client as c:
        missing = await c.get(f"/api/v1/branch/public/{BID}")
        garbage = await c.get("/api/v1/branch/public/not-a-uuid")

    assert missing.status_code == 404
    assert garbage.status_code == 422
