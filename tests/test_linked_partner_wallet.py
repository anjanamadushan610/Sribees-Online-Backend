"""Money and ownership invariants. Real Postgres tests roll back all fixtures."""
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from app.api.v1.linked_partner_wallet import AmountBody, WithdrawalBody, rollout_enabled
from app.models.partner import Partner
from app.models.partner_wallet import PartnerWallet, PartnerWalletEntry
from app.models.user import User
from app.models.wallet import Wallet
from app.services.partner_wallet_service import PartnerWalletService as Service
from app.services.partner_wallet_service import money
from app.services.referral_service import ReferralError, ReferralService
from tests.conftest import requires_db


def test_invalid_money_and_reverse_payload_are_rejected():
    for invalid in ['0', '-1', 'NaN', 'Infinity', '1.001', '1000000000']:
        with pytest.raises(HTTPException):
            money(invalid)
    assert money('10.50') == Decimal('10.50')
    with pytest.raises(ValidationError):
        AmountBody(request_id=uuid4(), amount='10', direction='sribees_to_partner')


def test_rollout_is_closed_by_default(monkeypatch):
    monkeypatch.delenv('LINKED_PARTNER_WALLET_ENABLED', raising=False)
    with pytest.raises(HTTPException) as error:
        rollout_enabled()
    assert error.value.status_code == 404


@pytest.fixture
async def money_db(db_schema):
    from app.config.settings import settings
    engine = create_async_engine(settings.async_database_url, poolclass=NullPool)
    async with engine.connect() as connection:
        outer = await connection.begin()
        async with AsyncSession(bind=connection, expire_on_commit=False, join_transaction_mode='create_savepoint') as db:
            suffix = uuid4().hex[:8]
            user = User(email=f'{suffix}@example.test', full_name='Kamal Test', referral_code=f'T{suffix}',
                        is_active=True, is_verified=True, is_blocked=False)
            db.add(user)
            await db.flush()
            db.add(Wallet(user_id=user.user_id, balance=Decimal('100.00')))
            await db.flush()
            yield db, user
        await outer.rollback()
    await engine.dispose()


async def linked_partner(db, user):
    partner = Partner(user_id=user.user_id, role='partner', email=user.email, full_name=user.full_name,
                      password_hash='unused-test-hash', referral_code=user.referral_code, is_active=True)
    db.add(partner)
    await db.flush()
    db.add(PartnerWallet(partner_id=partner.partner_id, balance=Decimal('100.00')))
    await db.flush()
    return partner


@requires_db
@pytest.mark.asyncio
async def test_only_approved_upgrade_preserves_code_customers_and_old_credit(money_db):
    db, user = money_db
    friend = User(email=f'{uuid4().hex}@example.test', full_name='Friend', referred_by_user_id=user.user_id)
    db.add(friend)
    await db.flush()
    code = user.referral_code
    application = await Service.apply(db, user.user_id, 'test-application-password')
    assert await Service.linked(db, user.user_id) is None
    partner = await Service.decide_application(db, application.application_id, uuid4(), True)
    await db.refresh(friend)
    assert partner.user_id == user.user_id
    assert partner.role == 'partner'  # no assigned master does NOT grant recruitment
    assert partner.referral_code == code == user.referral_code
    assert friend.referred_by_partner_id == partner.partner_id
    assert friend.referred_by_user_id is None
    assert await db.scalar(select(Wallet.balance).where(Wallet.user_id == user.user_id)) == Decimal('100.00')
    with pytest.raises(HTTPException):
        await Service.decide_application(db, application.application_id, uuid4(), True)
    with pytest.raises(ReferralError):
        await ReferralService.bind_referrer(db, user, code, commit=False)


@requires_db
@pytest.mark.asyncio
async def test_one_way_transfer_is_atomic_idempotent_and_cannot_overspend(money_db):
    db, user = money_db
    partner = await linked_partner(db, user)
    request_id = uuid4()
    await Service.transfer_to_shopping(db, partner, Decimal('30.00'), request_id)
    await Service.transfer_to_shopping(db, partner, Decimal('30.00'), request_id)
    assert await db.scalar(select(Wallet.balance).where(Wallet.user_id == user.user_id)) == Decimal('130.00')
    assert await db.scalar(select(PartnerWallet.balance).where(PartnerWallet.partner_id == partner.partner_id)) == Decimal('70.00')
    with pytest.raises(HTTPException):
        await Service.transfer_to_shopping(db, partner, Decimal('31.00'), request_id)
    with pytest.raises(HTTPException):
        await Service.transfer_to_shopping(db, partner, Decimal('71.00'), uuid4())


@requires_db
@pytest.mark.asyncio
async def test_withdrawal_reserves_money_and_rejection_releases_once(money_db):
    db, user = money_db
    partner = await linked_partner(db, user)
    body = WithdrawalBody(request_id=uuid4(), amount='90.00', bank_name='Test Bank', branch_name='Test',
                          account_name='Kamal Test', account_number='123456')
    withdrawal = await Service.withdraw(db, partner, body)
    assert (await Service.withdraw(db, partner, body)).withdrawal_id == withdrawal.withdrawal_id
    with pytest.raises(HTTPException):
        await Service.transfer_to_shopping(db, partner, Decimal('11.00'), uuid4())
    await Service.decide_withdrawal(db, withdrawal.withdrawal_id, uuid4(), False, None, 'Rejected test')
    assert await db.scalar(select(PartnerWallet.balance).where(PartnerWallet.partner_id == partner.partner_id)) == Decimal('100.00')
    with pytest.raises(HTTPException):
        await Service.decide_withdrawal(db, withdrawal.withdrawal_id, uuid4(), False, None, 'Retry')


@requires_db
@pytest.mark.asyncio
async def test_paid_withdrawal_does_not_debit_twice_and_requires_bank_reference(money_db):
    db, user = money_db
    partner = await linked_partner(db, user)
    row = await Service.withdraw(db, partner, WithdrawalBody(request_id=uuid4(), amount='20.00',
        bank_name='Test', branch_name='Test', account_name='Test', account_number='123456'))
    with pytest.raises(HTTPException):
        await Service.decide_withdrawal(db, row.withdrawal_id, uuid4(), True, '', None)
    await Service.decide_withdrawal(db, row.withdrawal_id, uuid4(), True, 'test-bank-reference', None)
    assert await db.scalar(select(PartnerWallet.balance).where(PartnerWallet.partner_id == partner.partner_id)) == Decimal('80.00')
    assert row.status == 'paid'


@requires_db
@pytest.mark.asyncio
async def test_partner_clawback_never_debits_shopping_credit(money_db):
    db, user = money_db
    partner = await linked_partner(db, user)
    wallet = await Service.wallet_lock(db, partner.partner_id)
    commission = SimpleNamespace(commission_id=uuid4(), beneficiary_partner_id=partner.partner_id, amount=Decimal('25.00'))
    await Service.entry(db, wallet, f'commission:{commission.commission_id}', 'commission', Decimal('30.00'))
    await Service.adjust_commission(db, commission, Decimal('5.00'))
    await Service.adjust_commission(db, commission, Decimal('5.00'))
    assert wallet.balance == Decimal('125.00')
    assert await db.scalar(select(Wallet.balance).where(Wallet.user_id == user.user_id)) == Decimal('100.00')
    entries = (await db.execute(select(PartnerWalletEntry).where(PartnerWalletEntry.partner_id == partner.partner_id))).scalars().all()
    assert len(entries) == 2


@requires_db
@pytest.mark.asyncio
async def test_failed_shopping_credit_rolls_back_partner_debit(money_db, monkeypatch):
    from app.services.wallet_service import WalletService
    db, user = money_db
    partner = await linked_partner(db, user)

    async def fail_credit(*args, **kwargs):
        raise RuntimeError('Simulated shopping ledger failure')

    monkeypatch.setattr(WalletService, 'apply_transaction', fail_credit)
    with pytest.raises(RuntimeError):
        async with db.begin_nested():
            await Service.transfer_to_shopping(db, partner, Decimal('30.00'), uuid4())
    assert await db.scalar(select(PartnerWallet.balance).where(PartnerWallet.partner_id == partner.partner_id)) == Decimal('100.00')
    assert await db.scalar(select(Wallet.balance).where(Wallet.user_id == user.user_id)) == Decimal('100.00')


@requires_db
@pytest.mark.asyncio
async def test_upgrade_moves_unpaid_commissions_but_never_paid_shopping_credit(money_db):
    from datetime import datetime, timedelta, timezone

    from app.models.order import Order
    from app.models.referral import ReferralCommission
    db, user = money_db
    commissions = []
    for status in ['pending', 'claimable', 'paid']:
        order = Order(user_id=user.user_id, order_number=f'TEST-{uuid4().hex}', subtotal=1000, total_amount=1000, status='delivered')
        db.add(order)
        await db.flush()
        row = ReferralCommission(order_id=order.order_id, beneficiary_user_id=user.user_id,
            source_user_id=user.user_id, level=1, rate_percentage=3, order_subtotal=1000, amount=30,
            status=status, claimable_at=datetime.now(timezone.utc) + timedelta(days=1 if status == 'pending' else -1))
        db.add(row)
        commissions.append(row)
    await db.flush()
    application = await Service.apply(db, user.user_id, 'test-password-for-application')
    partner = await Service.decide_application(db, application.application_id, uuid4(), True)
    for row in commissions:
        await db.refresh(row)
    assert commissions[0].beneficiary_partner_id == partner.partner_id
    assert commissions[0].status == 'pending'
    assert commissions[1].beneficiary_partner_id == partner.partner_id
    assert commissions[1].status == 'paid'
    assert commissions[2].beneficiary_user_id == user.user_id
    assert await db.scalar(select(PartnerWallet.balance).where(PartnerWallet.partner_id == partner.partner_id)) == Decimal('30.00')
    assert await db.scalar(select(Wallet.balance).where(Wallet.user_id == user.user_id)) == Decimal('100.00')
    assert await Service.credit_available(db, partner.partner_id) == 0


@requires_db
@pytest.mark.asyncio
async def test_two_concurrent_transfers_cannot_spend_same_balance(db_schema, monkeypatch):
    import asyncio

    from sqlalchemy import delete
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.config.settings import settings
    from app.models.wallet import WalletTransaction
    engine = create_async_engine(settings.async_database_url, poolclass=NullPool)
    if 'test' not in (engine.url.database or '').lower():
        await engine.dispose()
        pytest.skip('Concurrent committed fixtures run only on an explicitly named test database')
    monkeypatch.setattr(settings, 'finance_outbox_enabled', False)
    session = async_sessionmaker(engine, expire_on_commit=False)
    user_id, partner_id = uuid4(), uuid4()
    try:
        async with session() as db:
            db.add(User(user_id=user_id, full_name='Concurrent Test', email=f'{user_id}@example.test', is_active=True))
            await db.flush()
            db.add(Partner(partner_id=partner_id, user_id=user_id, role='partner', email=f'{user_id}@example.test', full_name='Test', password_hash='unused'))
            await db.flush()
            db.add(PartnerWallet(partner_id=partner_id, balance=100))
            db.add(Wallet(user_id=user_id, balance=100))
            await db.commit()

        async def transfer():
            async with session() as db:
                try:
                    partner = await db.get(Partner, partner_id)
                    await Service.transfer_to_shopping(db, partner, Decimal('70.00'), uuid4())
                    await db.commit()
                    return True
                except HTTPException:
                    await db.rollback()
                    return False

        assert sorted(await asyncio.gather(transfer(), transfer())) == [False, True]
        async with session() as db:
            assert await db.scalar(select(PartnerWallet.balance).where(PartnerWallet.partner_id == partner_id)) == Decimal('30.00')
            assert await db.scalar(select(Wallet.balance).where(Wallet.user_id == user_id)) == Decimal('170.00')
    finally:
        async with session() as db:
            await db.execute(delete(PartnerWalletEntry).where(PartnerWalletEntry.partner_id == partner_id))
            await db.execute(delete(WalletTransaction).where(WalletTransaction.user_id == user_id))
            await db.execute(delete(PartnerWallet).where(PartnerWallet.partner_id == partner_id))
            await db.execute(delete(Partner).where(Partner.partner_id == partner_id))
            await db.execute(delete(Wallet).where(Wallet.user_id == user_id))
            await db.execute(delete(User).where(User.user_id == user_id))
            await db.commit()
        await engine.dispose()


@pytest.mark.asyncio
async def test_normal_partner_without_a_master_still_cannot_recruit():
    from app.api.v1.partner import recruit_partner
    from app.schemas.partner import PartnerCreateRequest
    body = PartnerCreateRequest(email='new@example.com', password='test-password', full_name='Test Recruit')
    with pytest.raises(HTTPException) as error:
        await recruit_partner(body, db=None, partner=SimpleNamespace(role='partner', parent_partner_id=None))
    assert error.value.status_code == 403


@pytest.mark.asyncio
async def test_master_cannot_bypass_super_admin_approval_when_rollout_enabled(monkeypatch):
    from app.api.v1.partner import recruit_partner
    from app.schemas.partner import PartnerCreateRequest
    monkeypatch.setenv('LINKED_PARTNER_WALLET_ENABLED', 'true')
    body = PartnerCreateRequest(email='new@example.com', password='test-password', full_name='Test Recruit')
    with pytest.raises(HTTPException) as error:
        await recruit_partner(body, db=None, partner=SimpleNamespace(role='master_partner', parent_partner_id=None))
    assert error.value.status_code == 409


@requires_db
@pytest.mark.asyncio
async def test_old_order_replay_after_upgrade_cannot_generate_a_second_cash_commission(money_db, monkeypatch):
    from unittest.mock import AsyncMock

    from app.models.order import Order
    from app.models.referral import ReferralCommission
    db, user = money_db
    friend = User(email=f'{uuid4().hex}@example.test', full_name='Old Customer', referred_by_user_id=user.user_id)
    db.add(friend)
    await db.flush()
    order = Order(user_id=friend.user_id, order_number=f'TEST-{uuid4().hex}', subtotal=1000, total_amount=1000, status='delivered')
    db.add(order)
    await db.flush()
    db.add(ReferralCommission(order_id=order.order_id, source_user_id=friend.user_id, beneficiary_user_id=user.user_id,
        level=1, rate_percentage=3, order_subtotal=1000, amount=30, status='paid'))
    await db.flush()
    application = await Service.apply(db, user.user_id, 'test-password-for-application')
    await Service.decide_application(db, application.application_id, uuid4(), True)
    sale_lines = AsyncMock(side_effect=AssertionError('An old order must not be re-priced or re-accrued'))
    monkeypatch.setattr(ReferralService, '_sale_lines', sale_lines)
    assert await ReferralService.accrue_for_order(db, order) == []
    sale_lines.assert_not_called()
    assert await db.scalar(select(Wallet.balance).where(Wallet.user_id == user.user_id)) == Decimal('100.00')


@requires_db
@pytest.mark.asyncio
async def test_open_return_cannot_be_cashed_out_and_rejection_releases_it_once(money_db):
    from datetime import datetime, timedelta, timezone

    from app.models.order import Order
    from app.models.referral import ReferralCommission
    db, user = money_db
    partner = await linked_partner(db, user)
    order = Order(user_id=user.user_id, order_number=f'TEST-{uuid4().hex}', subtotal=1000,
                  total_amount=1000, status='return_requested')
    db.add(order)
    await db.flush()
    db.add(ReferralCommission(order_id=order.order_id, beneficiary_partner_id=partner.partner_id,
        source_user_id=user.user_id, level=1, rate_percentage=3, order_subtotal=1000, amount=30,
        status='claimable', claimable_at=datetime.now(timezone.utc) - timedelta(days=1)))
    await db.flush()
    assert await Service.credit_available(db, partner.partner_id) == 0
    assert (await Service.snapshot(db, partner))['pendingCommission'] == '30.00'
    order.status = 'return_rejected'
    await db.flush()
    assert await Service.credit_available(db, partner.partner_id) == 1
    assert await Service.credit_available(db, partner.partner_id) == 0
    assert await db.scalar(select(PartnerWallet.balance).where(PartnerWallet.partner_id == partner.partner_id)) == Decimal('130.00')


@requires_db
@pytest.mark.asyncio
async def test_finance_transfer_has_both_sides_and_stable_identity(money_db, monkeypatch):
    from app.config.settings import settings
    from app.models.finance_outbox import FinanceOutbox

    monkeypatch.setattr(settings, 'finance_outbox_enabled', True)
    db, user = money_db
    partner = await linked_partner(db, user)
    request_id = uuid4()
    await Service.transfer_to_shopping(db, partner, Decimal('30.00'), request_id)
    await db.flush()
    events = list((await db.scalars(select(FinanceOutbox))).all())
    shopping = [e.payload for e in events if e.event_type == 'wallet.movement'
                and e.payload.get('partner_transfer_id') == str(request_id)]
    assert len(shopping) == 1
    assert shopping[0]['partner_id'] == str(partner.partner_id)
    assert shopping[0]['amount'] == '30.00'
    partner_side = [e.payload for e in events if e.event_type == 'partner.wallet.movement'
                    and e.payload.get('reference') == f'shopping:{request_id}']
    assert len(partner_side) == 1
    assert partner_side[0]['amount'] == '-30.00'
    await Service.transfer_to_shopping(db, partner, Decimal('30.00'), request_id)
    await db.flush()
    assert len(list((await db.scalars(select(FinanceOutbox))).all())) == len(events)


@requires_db
@pytest.mark.asyncio
async def test_export_real_partner_lifecycle_for_finance_consumer(money_db, monkeypatch, capsys):
    """Export synthetic, rolled-back producer events for the other service's CI."""
    import json
    from datetime import datetime, timedelta, timezone
    from pathlib import Path

    from app.config.settings import settings
    from app.models.finance_outbox import FinanceOutbox
    from app.models.order import Order
    from app.models.referral import ReferralCommission

    monkeypatch.setattr(settings, 'finance_outbox_enabled', True)
    db, user = money_db
    partner = await linked_partner(db, user)
    wallet = await Service.wallet_lock(db, partner.partner_id)
    wallet.balance = Decimal('0.00')
    order = Order(user_id=user.user_id, order_number=f'CONTRACT-{uuid4().hex}',
                  subtotal=1000, total_amount=1000, status='delivered')
    db.add(order)
    await db.flush()
    commission = ReferralCommission(order_id=order.order_id, beneficiary_partner_id=partner.partner_id,
        source_user_id=user.user_id, level=1, rate_percentage=10, order_subtotal=1000,
        amount=100, status='claimable', claimable_at=datetime.now(timezone.utc) - timedelta(days=1))
    db.add(commission)
    await db.flush()
    assert await Service.credit_available(db, partner.partner_id) == 1
    transfer_id = uuid4()
    await Service.transfer_to_shopping(db, partner, Decimal('30.00'), transfer_id)
    rejected = await Service.withdraw(db, partner, WithdrawalBody(request_id=uuid4(), amount='20.00',
        bank_name='Synthetic Test Bank', branch_name='Test', account_name='Test', account_number='000000'))
    await Service.decide_withdrawal(db, rejected.withdrawal_id, uuid4(), False, None, 'Contract test')
    paid = await Service.withdraw(db, partner, WithdrawalBody(request_id=uuid4(), amount='10.00',
        bank_name='Synthetic Test Bank', branch_name='Test', account_name='Test', account_number='000000'))
    await Service.decide_withdrawal(db, paid.withdrawal_id, uuid4(), True, 'CONTRACT-PAID-10', None)
    commission.amount = Decimal('85.00')
    await Service.adjust_commission(db, commission, Decimal('15.00'))
    await db.flush()
    all_events = list((await db.scalars(select(FinanceOutbox).order_by(FinanceOutbox.seq))).all())
    events = [e for e in all_events if (
        e.event_type.startswith('partner.') and e.aggregate_id == partner.partner_id
        or e.event_type == 'commission.changed' and e.payload.get('commission_id') == str(commission.commission_id)
        or e.event_type == 'wallet.movement' and e.payload.get('partner_transfer_id') == str(transfer_id))]
    assert len(events) == 11
    assert all(e.payload['settlement_mode'] == 'partner_wallet'
               for e in events if e.event_type == 'commission.changed')
    assert wallet.balance == Decimal('45.00')
    assert await db.scalar(select(Wallet.balance).where(Wallet.user_id == user.user_id)) == Decimal('130.00')
    bundle = {'description': 'Synthetic Online producer events; no real customers, bank details or secrets',
        'partner_id': str(partner.partner_id), 'withdrawal_id': str(paid.withdrawal_id),
        'events': [{'seq': e.seq, 'event_id': str(e.event_id), 'type': e.event_type,
            'aggregate_id': str(e.aggregate_id) if e.aggregate_id else None,
            'occurred_at': e.occurred_at.isoformat(), 'payload': e.payload} for e in events],
        'expected': {'available': '45.00', 'reserved': '0.00', 'shopping_credit': '30.00',
                     'commission_expense': '85.00', 'bank_clearing': '10.00', 'transfer_clearing': '0.00'}}
    target = Path('reports/online-partner-contract.json')
    target.parent.mkdir(exist_ok=True)
    target.write_text(json.dumps(bundle, indent=2) + '\n')
    # Only this synthetic fixture: permit cross-service verification when artifact quota is full.
    with capsys.disabled():
        print('ONLINE_PARTNER_CONTRACT=' + json.dumps(bundle, separators=(',', ':')))
