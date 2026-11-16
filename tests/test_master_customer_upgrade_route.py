"""Exercise the URL used by the Partner Portal through FastAPI routing."""
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI

from app.api.v1.linked_partner_wallet import rollout_enabled, router
from app.config.database import get_db
from app.config.redis import get_redis
from app.core.dependencies import get_current_partner
from app.services.partner_wallet_service import PartnerWalletService


@pytest.mark.asyncio
async def test_portal_upgrade_url_reaches_role_and_consent_checks(monkeypatch):
    app = FastAPI()
    app.include_router(router, prefix='/api/v1')
    db = AsyncMock()
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_redis] = lambda: AsyncMock()
    app.dependency_overrides[rollout_enabled] = lambda: None
    app.dependency_overrides[get_current_partner] = lambda: SimpleNamespace(role='partner')
    upgrade = AsyncMock()
    monkeypatch.setattr(PartnerWalletService, 'master_upgrade', upgrade)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        url = f'/api/v1/partner/customers/{uuid4()}/upgrade'
        denied = await client.post(url, json={'customer_consent_confirmed': True})
        assert denied.status_code == 403
        missing_consent = await client.post(url, json={'customer_consent_confirmed': False})
        assert missing_consent.status_code == 422
    upgrade.assert_not_awaited()
    db.commit.assert_not_awaited()
