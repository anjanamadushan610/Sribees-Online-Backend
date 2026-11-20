"""Upgraded customers remain attributable in the direct customer directory."""
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from app.models.partner import Partner
from app.services.partner_service import PartnerService
from app.services.partner_wallet_service import PartnerWalletService
from tests.conftest import requires_db
from tests.test_linked_partner_wallet import money_db as money_db


@pytest.mark.asyncio
@pytest.mark.parametrize('completed_by', ['pending', 'password', 'login'])
async def test_customer_directory_returns_role_and_own_order_earnings(completed_by):
    customer_id, master_id = uuid4(), uuid4()
    db = AsyncMock()
    db.execute.return_value = MagicMock(all=lambda: [
        (customer_id, 'Promoted customer', None, Decimal('80'), 2, 'partner',
         datetime.now(timezone.utc) if completed_by == 'password' else None,
         datetime.now(timezone.utc) if completed_by == 'login' else None),
    ])
    rows = await PartnerService.list_customers(db, master_id)
    assert rows[0]['partner_role'] == 'partner'
    assert rows[0]['password_setup_completed'] is (completed_by != 'pending')
    assert rows[0]['orders'] == 2
    assert rows[0]['total_earned'] == 80
    query = db.execute.call_args.args[0]
    sql = str(query.compile(compile_kwargs={'literal_binds': True}))
    assert 'partners.partner_id IS NULL' not in sql
    assert 'referral_commissions.level = 1' in sql
    assert master_id.hex in sql


@requires_db
@pytest.mark.asyncio
async def test_upgrade_retains_customer_in_own_master_directory(money_db):  # noqa: F811
    db, customer = money_db
    master = Partner(full_name='Directory master', email=f'{uuid4().hex}@example.test',
                     role='master_partner', password_hash='unused', is_active=True)
    db.add(master)
    await db.flush()
    customer.referred_by_partner_id = master.partner_id
    await db.flush()
    before = await PartnerService.list_customers(db, master.partner_id)
    assert before[0]['partner_role'] is None
    created = await PartnerWalletService.master_upgrade(db, customer.user_id, master.partner_id, 'SyntheticPassword123!')
    after = await PartnerService.list_customers(db, master.partner_id)
    assert after[0]['user_id'] == str(customer.user_id)
    assert after[0]['partner_role'] == 'partner'
    assert after[0]['password_setup_completed'] is False
    from app.services.partner_invitation import accept_invitation, setup_url
    token = setup_url(created).split('#token=', 1)[1]
    await accept_invitation(db, token, 'SyntheticNewPassword123!')
    completed = await PartnerService.list_customers(db, master.partner_id)
    assert completed[0]['password_setup_completed'] is True
    assert await PartnerService.list_customers(db, uuid4()) == []
